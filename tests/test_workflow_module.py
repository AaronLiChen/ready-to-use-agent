"""Portable installation and platform tests use isolated homes and fake providers."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_install import installer, ROOT
sys.path.insert(0, str(ROOT / 'workflow'))
import workflow as w
import configuration
import service
import validation
import write_guard


class WorkflowModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='portable-workflow-home-')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        self.home = Path(self.temp.name)
        patch.object(w, 'ROOT', self.home / '.agents/ready-to-use-agent/state/workflow').start()
        patch.object(w, 'notify').start()
        patch.dict(os.environ, {'AUTOWORKFLOW_ROOT': str(w.ROOT), 'AUTOWORKFLOW_JOB': '',
                               'AUTOWORKFLOW_INTAKE': '', 'AUTOWORKFLOW_AUTH_ID': '', 'AUTOWORKFLOW_GITHUB_LOGIN': ''}).start()

    def install(self, **kwargs):
        value = installer.Installation(self.home, workflow=True, workflow_platform='linux', **kwargs)
        value.apply()
        return value

    def test_default_core_install_has_no_controller_or_service(self):
        installer.Installation(self.home).apply()
        self.assertFalse((self.home / '.local/bin/agent-workflow').exists())
        self.assertFalse((self.home / '.config/systemd').exists())

    def test_workflow_dry_run_starts_nothing_and_writes_nothing(self):
        with patch.object(subprocess, 'run') as execute:
            plan = installer.Installation(self.home, workflow=True, workflow_platform='linux').plan()
        self.assertGreater(len(plan), 30)
        self.assertEqual(list(self.home.iterdir()), [])
        execute.assert_not_called()

    def test_installed_controller_and_intake_have_disabled_defaults(self):
        with patch.object(subprocess, 'run') as execute:
            self.install()
        execute.assert_not_called()
        self.assertFalse(configuration.controller()['background_enabled'])
        self.assertEqual(configuration.delegation(), {'comments': False, 'approvals': False})
        for name in ('ado', 'github', 'teams'):
            self.assertFalse(w.load(w.ROOT / (name + '-config.json'))['enabled'])
        self.assertFalse((w.ROOT / 'jobs').exists())
        self.assertFalse((w.ROOT / 'sessions').exists())

    def test_linux_units_use_destination_paths_without_activation(self):
        self.install()
        text = (self.home / '.config/systemd/user/ready-agent-workflow.service').read_text()
        timer = (self.home / '.config/systemd/user/ready-agent-workflow.timer').read_text()
        self.assertIn(str(self.home), text)
        self.assertIn('UMask=0077', text)
        self.assertIn('OnUnitActiveSec=60', timer)
        self.assertTrue(service.manifest())

    def test_macos_plist_is_inert_and_uses_destination_paths(self):
        installer.Installation(self.home, workflow=True, workflow_platform='darwin').apply()
        path = self.home / 'Library/LaunchAgents/org.ready-to-use-agent.workflow.plist'
        data = plistlib.loads(path.read_bytes())
        self.assertFalse(data['RunAtLoad'])
        self.assertTrue(data['Disabled'])
        self.assertEqual(data['StartInterval'], 60)
        self.assertIn(str(self.home), data['ProgramArguments'][-2])

    def test_macos_activation_clears_the_disabled_flag_only_after_opt_in(self):
        installer.Installation(self.home, workflow=True, workflow_platform='darwin').apply()
        def native(args, **kwargs):
            return SimpleNamespace(returncode=1 if args[:2] == ['launchctl', 'print'] else 0, stdout='')
        with patch.object(subprocess, 'run', side_effect=native) as execute:
            self.assertEqual(service.change('enable')['state'], 'enabled')
        self.assertTrue(any(call.args[0][:2] == ['launchctl', 'enable'] for call in execute.call_args_list))
        self.assertTrue(any(call.args[0][:2] == ['launchctl', 'bootstrap'] for call in execute.call_args_list))

    def test_macos_activation_does_not_take_over_another_service(self):
        installer.Installation(self.home, workflow=True, workflow_platform='darwin').apply()
        with patch.object(subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='program = /other/controller')) as execute:
            with self.assertRaisesRegex(ValueError, 'Another registered service'):
                service.change('enable')
        self.assertEqual(execute.call_count, 1)
        self.assertFalse(configuration.controller()['background_enabled'])

    def test_native_windows_refuses_controller_without_partial_install(self):
        with self.assertRaisesRegex(ValueError, 'Windows'):
            installer.Installation(self.home, workflow=True, workflow_platform='win32').apply()
        self.assertEqual(list(self.home.iterdir()), [])
        installer.Installation(self.home, workflow_platform='win32').apply()
        self.assertTrue((self.home / '.pi/agent/AGENTS.md').exists())

    def test_destination_registry_contains_no_imported_jobs_or_credentials(self):
        self.install(workflow_pi=sys.executable)
        registry = w.load(self.home / '.agents/ready-to-use-agent/clients.json')
        self.assertEqual(registry['backend'], 'pi')
        self.assertIn(str(Path(sys.executable).resolve()), registry['clients'][0]['executables'])
        self.assertFalse(list(self.home.rglob('auth.json')))
        self.assertFalse(list(self.home.rglob('*.jsonl')))

    def test_private_source_files_are_not_in_the_allowlist(self):
        support = installer.Installation(self.home).workflow_support()
        for name in ('auth.json', 'audit_history.py', 'jobs/live.json', 'silent_reader/vendor.dll', 'task-prompt.md'):
            self.assertNotIn(name, support.FILES)
        self.install()
        names = list(w.load(self.home / '.agents/ready-to-use-agent/install-manifest.json')['files'])
        self.assertFalse(any('audit_history' in name or '/jobs/' in name or '.dll' in name for name in names))

    def test_existing_live_controller_requires_coordinated_migration(self):
        path = self.home / '.pi/workflows/autoworkflow'
        path.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'competing'):
            self.install()
        self.assertFalse((self.home / '.local/bin/agent-workflow').exists())

    def test_unmanaged_cli_is_not_overwritten(self):
        path = self.home / '.local/bin/agent-workflow'
        path.parent.mkdir(parents=True)
        path.write_text('Preserve the existing controller.')
        with self.assertRaisesRegex(ValueError, 'unmanaged'):
            self.install()
        self.assertEqual(path.read_text(), 'Preserve the existing controller.')

    def test_edited_controller_is_not_overwritten(self):
        self.install()
        path = self.home / '.agents/ready-to-use-agent/workflow/workflow.py'
        path.write_text('Operator edit.')
        with self.assertRaisesRegex(ValueError, 'edited'):
            self.install()
        self.assertEqual(path.read_text(), 'Operator edit.')

    def test_operator_identity_configuration_survives_reinstall(self):
        self.install()
        path = w.ROOT / 'github-config.json'
        data = w.load(path)
        data['logins'] = ['ExampleOperator']
        w.save(path, data)
        self.install()
        self.assertEqual(w.load(path)['logins'], ['ExampleOperator'])

    def test_intake_activation_requires_an_explicit_identity(self):
        self.install()
        for kind in ('github', 'ado', 'teams'):
            with self.assertRaises(ValueError): configuration.activate_intake(kind)
        w.save(w.ROOT / 'github-config.json', {'logins': ['ExampleOperator'], 'enabled': False})
        configuration.activate_intake('github')
        self.assertTrue(w.load(w.ROOT / 'github-config.json')['enabled'])
        self.assertEqual(configuration.delegation(), {'comments': False, 'approvals': False})

    def test_disabled_service_has_no_provider_or_model_calls(self):
        self.install()
        with patch.object(subprocess, 'Popen') as spawn, patch.object(subprocess, 'run') as execute:
            self.assertEqual(service.run()['state'], 'disabled')
        spawn.assert_not_called()
        execute.assert_not_called()

    def test_failed_intake_does_not_stop_other_providers_or_owned_work(self):
        self.install()
        config = configuration.controller()
        config['background_enabled'] = True
        w.save(w.ROOT / 'controller-config.json', config)
        adapters = {kind: SimpleNamespace(poll=Mock())
                    for kind in ('ado', 'github', 'teams')}
        adapters['ado'].poll.side_effect = ValueError('Synthetic configuration error')
        import auth_recovery, github_auth, parallel_dispatch
        with patch.object(auth_recovery, 'tick'), patch.object(github_auth, 'health_tick'), \
             patch.object(w, 'intake_adapter', side_effect=lambda request: adapters[request['source_kind']]), \
             patch.object(parallel_dispatch, 'run') as dispatch:
            result = service.run()
        adapters['github'].poll.assert_called_once()
        adapters['teams'].poll.assert_called_once()
        dispatch.assert_called_once()
        self.assertEqual(result['intake_errors'], {'ado': 'ValueError'})

    def test_source_only_evidence_requires_the_exact_source_and_live_policy(self):
        self.install()
        head = 'a' * 40
        job = {'id': 'fixture', 'mode': 'review', 'status': 'active', 'workspace': str(self.home)}
        w.save(w.job_path('fixture'), job)
        proof = self.home / 'source-proof.json'
        proof.write_text(json.dumps({'policy': {'source': head, 'inputs_unchanged': True,
                                               'evidence': 'Verified tested source and unchanged validation inputs.'}}))
        data = {'head': head, 'policies': [{'id': 'policy', 'state': 'approved', 'context': {'isExpired': False},
                                          'settings': {'queueOnSourceUpdateOnly': True}}]}
        args = ['agent-workflow', 'review-evidence', 'fixture', '--head', head, '--evidence', 'Actual fixture review',
                '--source-only-evidence', str(proof)]
        with patch.dict(os.environ, {'AUTOWORKFLOW_JOB': 'fixture'}), patch.object(sys, 'argv', args), \
             patch.object(w, 'snapshot', return_value=data), patch.object(w, 'git', return_value=head), redirect_stdout(io.StringIO()):
            w.main()
            self.assertEqual(w.load(w.job_path('fixture'))['source_only_validation']['policy']['source'], head)
            proof.write_text(json.dumps({'policy': {'source': 'b' * 40, 'inputs_unchanged': True, 'evidence': 'Wrong source'}}))
            with self.assertRaisesRegex(ValueError, 'Source-only'):
                w.main()

    def test_activation_is_explicit_and_preserves_intake_delegation(self):
        self.install()
        with patch.object(subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as execute:
            self.assertEqual(service.change('enable')['state'], 'enabled')
        self.assertEqual(execute.call_args.args[0], ['systemctl', '--user', 'enable', '--now', 'ready-agent-workflow.timer'])
        self.assertEqual(configuration.delegation(), {'comments': False, 'approvals': False})
        self.assertFalse(w.load(w.ROOT / 'github-config.json')['enabled'])

    def test_failed_activation_does_not_claim_success(self):
        self.install()
        with patch.object(subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(RuntimeError): service.change('enable')
        self.assertFalse(configuration.controller()['background_enabled'])

    def test_service_edits_block_activation_without_overwriting(self):
        self.install()
        path = self.home / '.config/systemd/user/ready-agent-workflow.service'
        path.write_text('Operator service edit.')
        with self.assertRaisesRegex(ValueError, 'changed'): service.change('enable')
        self.assertEqual(path.read_text(), 'Operator service edit.')

    def test_uninstall_preserves_pending_task_ownership(self):
        self.install()
        w.save(w.job_path('owned'), {'id': 'owned', 'status': 'waiting'})
        with self.assertRaisesRegex(ValueError, 'ownership'):
            installer.Installation(self.home).uninstall()
        self.assertTrue((self.home / '.local/bin/agent-workflow').exists())

    def test_optional_claude_hooks_preserve_native_permissions_and_existing_hooks(self):
        path = self.home / '.claude/settings.json'
        path.parent.mkdir()
        before = {'permissions': {'deny': ['Bash(sudo:*)']}, 'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'existing-hook'}]}]}}
        path.write_text(json.dumps(before))
        self.install(agents=('pi', 'claude'), claude_hooks=True)
        data = w.load(path)
        self.assertEqual(data['permissions'], before['permissions'])
        self.assertEqual(data['hooks']['Stop'][0], before['hooks']['Stop'][0])
        self.assertEqual(len(data['hooks']['Stop']), 2)
        self.install(agents=('pi', 'claude'), claude_hooks=True)
        self.assertEqual(len(w.load(path)['hooks']['Stop']), 2)

    def test_hook_uninstall_preserves_unrelated_operator_edits(self):
        self.install(agents=('pi', 'claude'), claude_hooks=True)
        path = self.home / '.claude/settings.json'
        data = w.load(path)
        data['permissions'] = {'deny': ['Bash(sudo:*)']}
        w.save(path, data)
        installer.Installation(self.home).uninstall()
        data = w.load(path)
        self.assertEqual(data['permissions'], {'deny': ['Bash(sudo:*)']})
        self.assertEqual(data['hooks']['Stop'], [])

    def test_installed_cli_help_has_no_model_or_cloud_call(self):
        self.install()
        env = {**os.environ, 'HOME': str(self.home)}
        result = subprocess.run([str(self.home / '.local/bin/agent-workflow'), '--help'], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('review-evidence', result.stdout)
        self.assertFalse((w.ROOT / 'jobs').exists())

    def test_worker_loads_only_explicit_native_compaction(self):
        self.install()
        command = w.worker_command({'id': 'synthetic'})
        self.assertIn('--no-extensions', command)
        self.assertEqual(command.count('--extension'), 1)
        self.assertTrue(command[command.index('--extension') + 1].endswith('openai-native-compaction.ts'))
        self.assertFalse(any('ownership.ts' in value for value in command))

    def test_worker_environment_excludes_provider_write_credentials(self):
        env = configuration.clean_worker_env({'GH_TOKEN': 'synthetic', 'AZURE_DEVOPS_EXT_PAT': 'synthetic',
                                              'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_VALUE_0': 'synthetic', 'GIT_TRACE2_EVENT': 'synthetic',
                                              'GH_DEBUG': 'api', 'AZURE_LOG_LEVEL': 'debug', 'PATH': '/bin'})
        self.assertEqual(env, {'PATH': '/bin'})

    def test_source_only_review_requires_source_and_input_evidence(self):
        policy = {'blocking': True, 'type': 'Build', 'state': 'approved', 'context': {'buildIsNotCurrent': True},
                  'settings': {'queueOnSourceUpdateOnly': True}}
        self.assertTrue(validation.ado_policy(policy, 'a', 'a', True, review=True))
        self.assertFalse(validation.ado_policy(policy, 'a', 'b', True, review=True))
        self.assertFalse(validation.ado_policy(policy, 'a', 'a', False, review=True))
        self.assertFalse(validation.ado_policy(policy, 'a', 'a', True, review=False))

    def test_missing_skipped_or_stale_github_checks_are_not_passes(self):
        with self.assertRaises(ValueError): validation.github({'head': 'a', 'checks': []})
        for conclusion, sha in (('skipped', 'a'), ('success', 'b'), ('failure', 'a'), ('success', None)):
            with self.assertRaises(ValueError):
                validation.github({'head': 'a', 'checks': [{'name': 'CI', 'head_sha': sha, 'status': 'completed', 'conclusion': conclusion}]})

    def test_tracked_build_requires_a_known_source_or_verified_merge_candidate(self):
        build = {'status': 'completed', 'result': 'succeeded'}
        for source in (None, '', 'old'):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'not bound'):
                validation.tracked_builds({'head': 'current', 'extra_builds': [{**build, 'sourceVersion': source}]})
        self.assertTrue(validation.tracked_builds({'head': 'current', 'extra_builds': [{**build, 'sourceVersion': 'current'}]}))
        self.assertTrue(validation.tracked_builds({'head': 'current', 'merge_commit': 'verified',
                                                 'extra_builds': [{**build, 'sourceVersion': 'verified'}]}))

    def test_preparation_cannot_grant_itself_provider_write_authority(self):
        with patch.dict(os.environ, {'AUTOWORKFLOW_INTAKE': 'request'}):
            with self.assertRaisesRegex(ValueError, 'Preparation'): write_guard.assigned()

    def test_unsupported_merge_commands_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'merge'):
            write_guard.github_command(['pr', 'merge', 'https://github.com/example/repo/pull/1'])


if __name__ == '__main__':
    unittest.main()
