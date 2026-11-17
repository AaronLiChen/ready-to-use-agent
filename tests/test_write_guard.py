"""Exercise managed-command guards without credentials or provider operations."""
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_install import ROOT
sys.path.insert(0, str(ROOT / 'workflow'))
import workflow as w
import write_guard as guard


class CommandGuardTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.job = {'id': 'fixture', 'url': 'https://github.com/example/repo/pull/42',
                    'mode': 'review', 'reviewed_head': 'a' * 40}
        patch.object(guard, 'assigned', return_value=self.job).start()
        self.authorize = patch.object(guard, 'authorize', return_value={}).start()
        patch.dict(os.environ, {'AUTOWORKFLOW_EXPECTED_HEAD': ''}).start()

    def test_raw_api_mutation_flag_variants_are_blocked(self):
        for args in (
            ['api', 'repos/example/repo/pulls/42/reviews', '--method=POST'],
            ['api', '-XPOST', 'repos/example/repo/pulls/42/reviews'],
            ['api', 'repos/example/repo/pulls/42/reviews', '-fevent=APPROVE'],
            ['api', 'repos/example/repo/pulls/42/reviews', '--input=payload.json'],
            ['api', 'repos/example/repo/pulls/42/reviews', '--method', 'GET', '-f', 'event=APPROVE'],
            ['api', 'graphql', '-fquery=mutation { anything }'],
            ['api', 'graphql', '--method=GET'],
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                guard.github_command(args)
        self.authorize.assert_not_called()

    def test_rejected_commands_do_not_retrieve_credentials_or_use_a_global_account(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            native = root / 'native-gh'
            marker = root / 'credential-requested'
            native.write_text('#!' + sys.executable + '\nfrom pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("unexpected")\n')
            native.chmod(0o700)
            (root / 'controller-config.json').write_text(json.dumps({'version': 1, 'executables': {'gh': str(native)}}))
            env = {**os.environ, 'AUTOWORKFLOW_ROOT': str(root), 'AUTOWORKFLOW_JOB': 'fixture',
                   'AUTOWORKFLOW_GITHUB_LOGIN': 'Reviewer', 'AUTOWORKFLOW_INTAKE': ''}
            for login, args in (('Reviewer', ['auth', 'status', '--show-token']), ('', ['api', 'user'])):
                env['AUTOWORKFLOW_GITHUB_LOGIN'] = login
                result = subprocess.run([sys.executable, str(ROOT / 'workflow/bin/gh'), *args], env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 75, result.stderr)
                self.assertFalse(marker.exists())

    def test_readonly_api_forms_remain_available(self):
        for args in (
            ['api', '--paginate', '--slurp', 'repos/example/repo/pulls'],
            ['api', 'user', '--method=GET', '--jq', '.login'],
            ['api', '-XGET', 'https://api.github.com/user'],
        ):
            self.assertIsNone(guard.github_command(args))
        self.authorize.assert_not_called()

    def test_other_hosts_tokens_and_browser_actions_are_blocked(self):
        for args in (
            ['api', 'https://example.test/collect'],
            ['api', 'user', '--hostname=example.test'],
            ['auth', 'status', '--show-token'],
            ['auth', 'status', '-t'],
            ['pr', 'view', '42', '--web'],
            ['pr', 'view', '42', '--web=true'],
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                guard.github_command(args)

    def test_attached_approval_and_repository_flags_pin_the_reviewed_commit(self):
        result = guard.github_command(['pr', 'review', '42', '--repo=example/repo', '--approve=true', '-bReviewed source.'])
        self.authorize.assert_called_once_with('approval', 'a' * 40, self.job)
        self.assertIn('commit_id=' + 'a' * 40, result)
        self.assertIn('event=APPROVE', result)
        self.assertIn('body=Reviewed source.', result)
        self.assertEqual(result[1], 'repos/example/repo/pulls/42/reviews')

    def test_conflicting_review_modes_and_other_repositories_are_blocked(self):
        for extra in (['--approve', '--comment'], ['--approve=false'], ['--approve', '--repo=example/other']):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                guard.github_command(['pr', 'review', self.job['url'], *extra])

    def test_body_files_become_fixed_body_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'body.txt'
            path.write_text('Verified finding.\n')
            result = guard.github_command(['pr', 'comment', '42', '-Rexample/repo', '--body-file=' + str(path)])
        self.assertEqual(result, ['pr', 'comment', self.job['url'], '--body', 'Verified finding.\n'])
        self.authorize.assert_called_once_with('comment', 'a' * 40, self.job)

    def test_interactive_and_unscoped_mutations_are_blocked(self):
        for args in (
            ['pr', 'comment', self.job['url']],
            ['pr', 'review', self.job['url'], '--approve', '--body-file', '-'],
            ['pr', 'edit', self.job['url'], '--base', 'other'],
            ['pr', 'comment', self.job['url'], '--delete-last'],
            ['pr', 'merge', self.job['url']],
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                guard.github_command(args)

    def test_native_ado_mutation_variants_are_blocked(self):
        for args in (
            ['repos', 'pr', 'vote', '--id', '42', '--vote', 'approve'],
            ['devops', 'invoke', '--http-method=PUT'],
            ['devops', 'invoke', '--http-method', 'POST'],
            ['devops', 'invoke', '--http-m=POST'],
            ['devops', 'invoke', '--area', '--http-method=POST'],
            ['devops', 'invoke', '--in-file=payload.json'],
            ['rest', '--method', 'PUT'],
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                guard.azure_command(args)

    def test_native_ado_reads_remain_available(self):
        guard.azure_command(['repos', 'pr', 'show', '--id', '42'])
        guard.azure_command(['devops', 'invoke', '--area', 'git', '--resource', 'pullRequests',
                             '--route-parameters', 'project=Project', 'repositoryId=repo',
                             '--http-method=GET', '-o', 'json'])

    def test_git_global_options_cannot_redirect_a_managed_push(self):
        self.job.update(mode='implement', branch='owned', workspace='/synthetic/workspace', enrolled_remote='https://github.com/example/repo.git')
        def git_read(workspace, *args):
            return 'owned' if args[0] == 'branch' else self.job['enrolled_remote']
        with patch.object(w, 'git', side_effect=git_read), patch.object(Path, 'cwd', return_value=Path(self.job['workspace'])):
            for args in (['-C', '/other', 'push', 'origin', 'owned'], ['-c', 'remote.origin.url=other', 'push', 'origin', 'owned']):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    guard.git_push(args)
            guard.git_push(['push', 'origin', 'owned'])


class GuardOwnershipTests(unittest.TestCase):
    def test_disabled_desktop_notices_start_no_native_process(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(w, 'ROOT', Path(folder)), patch.object(subprocess, 'run') as execute:
            self.assertFalse(w.notify('Synthetic notice'))
            w.save(w.ROOT / 'action-notices-config.json', {'enabled': True, 'desktop': False})
            self.assertFalse(w.notify('Synthetic notice'))
            execute.assert_not_called()

    def test_managed_writes_require_the_actual_process_owner(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(w, 'ROOT', Path(folder)):
            expected = {'pid': 100, 'identity': 'synthetic-start-time'}
            w.save(w.job_path('fixture'), {'id': 'fixture', 'status': 'active', 'owner': expected})
            with patch.dict(os.environ, {'AUTOWORKFLOW_JOB': 'fixture', 'AUTOWORKFLOW_INTAKE': ''}):
                with patch.object(w, 'owner', return_value={'pid': 101, 'identity': 'other'}):
                    with self.assertRaisesRegex(ValueError, 'does not own'):
                        guard.assigned()
                with patch.object(w, 'owner', return_value=expected):
                    self.assertEqual(guard.assigned()['id'], 'fixture')


if __name__ == '__main__':
    unittest.main()
