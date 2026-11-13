import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ready_agent_install', ROOT / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ready-agent-home-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def install(self, **kwargs):
        value = installer.Installation(self.home, source=ROOT, **kwargs)
        value.apply()
        return value

    def test_dry_run_writes_nothing_and_starts_no_process(self):
        with patch.object(installer.subprocess, 'run') as process:
            plan = installer.Installation(self.home, source=ROOT).plan()
        self.assertTrue(plan)
        self.assertEqual(list(self.home.iterdir()), [])
        process.assert_not_called()

    def test_fresh_install_includes_compaction_but_no_credentials_or_sessions(self):
        self.install()
        settings = json.loads((self.home / '.pi/agent/settings.json').read_text())
        self.assertEqual(settings['defaultProjectTrust'], 'ask')
        self.assertEqual(settings['defaultThinkingLevel'], 'max')
        self.assertNotIn('defaultModel', settings)
        self.assertNotIn('defaultProvider', settings)
        self.assertTrue((self.home / '.pi/workflows/openai-native-compaction/recovery.ts').exists())
        self.assertTrue((self.home / '.agents/skills/ready-agent-setup/SKILL.md').exists())
        self.assertFalse(list(self.home.rglob('auth.json')))
        self.assertFalse(list(self.home.rglob('*.jsonl')))
        self.assertFalse((self.home / '.pi/state').exists())

    def test_repeated_install_has_one_managed_block_and_changes_no_files(self):
        self.install()
        second = installer.Installation(self.home, source=ROOT)
        result = second.apply()
        self.assertEqual(result['changed_files'], 0)
        text = (self.home / '.pi/agent/AGENTS.md').read_text()
        self.assertEqual(text.count(installer.START), 1)

    def test_existing_instructions_settings_and_credentials_remain_intact(self):
        directory = self.home / '.pi/agent'
        directory.mkdir(parents=True)
        (directory / 'AGENTS.md').write_text('Keep the existing task scope.\n')
        (directory / 'settings.json').write_text(json.dumps({'defaultProvider': 'existing', 'defaultModel': 'existing-model',
                                                            'permissions': {'confirmation': True}, 'theme': 'light'}))
        (directory / 'auth.json').write_text('PRIVATE_TEST_CREDENTIAL')
        self.install()
        settings = json.loads((directory / 'settings.json').read_text())
        self.assertEqual(settings['defaultModel'], 'existing-model')
        self.assertEqual(settings['defaultProvider'], 'existing')
        self.assertEqual(settings['permissions'], {'confirmation': True})
        self.assertEqual(settings['theme'], 'light')
        self.assertTrue((directory / 'AGENTS.md').read_text().startswith('Keep the existing task scope.'))
        self.assertEqual((directory / 'auth.json').read_text(), 'PRIVATE_TEST_CREDENTIAL')
        self.assertFalse(any('auth.json' in p.name for p in self.home.rglob('*') if p.is_file() and 'backups' in p.parts))

    def test_claude_and_codex_profiles_share_rules_without_copying_auth(self):
        self.install(agents=('pi', 'claude', 'codex'))
        self.assertTrue((self.home / '.claude/CLAUDE.md').exists())
        self.assertTrue((self.home / '.codex/AGENTS.md').exists())
        self.assertTrue((self.home / '.claude/skills/ready-agent-setup/SKILL.md').exists())
        self.assertFalse((self.home / '.claude/settings.json').exists())
        self.assertFalse((self.home / '.codex/config.toml').exists())

    def test_non_pi_profile_does_not_change_pi_configuration(self):
        self.install(agents=('claude',))
        self.assertFalse((self.home / '.pi').exists())

    def test_selected_model_is_explicit_and_threshold_is_bounded(self):
        self.install(provider='openai', model='gpt-example', threshold=150000)
        settings = json.loads((self.home / '.pi/agent/settings.json').read_text())
        self.assertEqual(settings['defaultProvider'], 'openai')
        self.assertEqual(settings['defaultModel'], 'gpt-example')
        self.assertIn('150000', (self.home / '.pi/agent/extensions/openai-native-compaction.ts').read_text())
        for values in ({'provider': 'openai'}, {'threshold': 0}, {'threshold': 1000001}):
            with self.assertRaises(ValueError):
                installer.Installation(self.home, source=ROOT, **values)

    def test_unmanaged_extension_conflict_aborts_without_partial_install(self):
        extension = self.home / '.pi/agent/extensions/openai-native-compaction.ts'
        extension.parent.mkdir(parents=True)
        extension.write_text('KEEP_UNMANAGED_EXTENSION')
        with self.assertRaisesRegex(ValueError, 'unmanaged'):
            self.install()
        self.assertEqual(extension.read_text(), 'KEEP_UNMANAGED_EXTENSION')
        self.assertFalse((self.home / '.agents/ready-to-use-agent').exists())

    def test_edited_managed_source_is_not_overwritten(self):
        self.install()
        path = self.home / '.pi/workflows/openai-native-compaction/core.ts'
        path.write_text(path.read_text() + '\n// Operator change.\n')
        with self.assertRaisesRegex(ValueError, 'edited'):
            self.install()
        self.assertTrue(path.read_text().endswith('// Operator change.\n'))

    def test_edited_managed_instruction_block_is_not_overwritten(self):
        self.install()
        path = self.home / '.pi/agent/AGENTS.md'
        path.write_text(path.read_text().replace('Installation grants no', 'Operator edited the block: no'))
        with self.assertRaisesRegex(ValueError, 'instruction block'):
            self.install()
        self.assertIn('Operator edited the block', path.read_text())

    def test_edits_outside_the_managed_block_survive_reinstallation(self):
        self.install()
        path = self.home / '.pi/agent/AGENTS.md'
        path.write_text('New operator rule.\n\n' + path.read_text())
        self.install()
        self.assertTrue(path.read_text().startswith('New operator rule.'))

    def test_symlink_targets_and_path_traversal_are_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            try:
                (self.home / '.pi').symlink_to(other, target_is_directory=True)
            except OSError:
                self.skipTest('This platform cannot create test symlinks')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                self.install()
            self.assertEqual(list(Path(other).iterdir()), [])
        for value in ('../../outside', '/absolute'):
            with self.assertRaisesRegex(ValueError, 'relative'):
                installer.Installation(self.home, source=ROOT).target(value)

    def test_uninstall_restores_original_files_and_preserves_operator_edits(self):
        path = self.home / '.pi/agent/AGENTS.md'
        path.parent.mkdir(parents=True)
        path.write_text('Original instructions.\n')
        self.install()
        core = self.home / '.pi/workflows/openai-native-compaction/core.ts'
        core.write_text('KEEP_OPERATOR_EDIT')
        result = installer.Installation(self.home, source=ROOT).uninstall()
        self.assertEqual(path.read_text(), 'Original instructions.\n')
        self.assertEqual(core.read_text(), 'KEEP_OPERATOR_EDIT')
        self.assertEqual(result['preserved_edited_files'], [str(core)])
        manifest = json.loads((self.home / '.agents/ready-to-use-agent/install-manifest.json').read_text())
        self.assertEqual(len(manifest['files']), 1)

    def test_clean_uninstall_removes_only_managed_files(self):
        self.install()
        unrelated = self.home / '.pi/agent/unrelated.txt'
        unrelated.write_text('KEEP')
        result = installer.Installation(self.home, source=ROOT).uninstall()
        self.assertFalse(result['preserved_edited_files'])
        self.assertEqual(unrelated.read_text(), 'KEEP')
        self.assertFalse((self.home / '.pi/agent/extensions/openai-native-compaction.ts').exists())

    def test_missing_backup_prevents_any_uninstall_change(self):
        path = self.home / '.pi/agent/AGENTS.md'
        path.parent.mkdir(parents=True)
        path.write_text('Original instructions.\n')
        self.install()
        manifest = json.loads((self.home / '.agents/ready-to-use-agent/install-manifest.json').read_text())
        Path(manifest['files']['.pi/agent/AGENTS.md']['backup']).unlink()
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'backup'):
            installer.Installation(self.home, source=ROOT).uninstall()
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue((self.home / '.pi/workflows/openai-native-compaction/core.ts').exists())

    def test_file_race_aborts_and_rolls_back_its_own_changes(self):
        install = installer.Installation(self.home, source=ROOT)
        install.plan()
        path = self.home / '.pi/agent/AGENTS.md'
        path.parent.mkdir(parents=True)
        path.write_text('Concurrent operator change.\n')
        with self.assertRaisesRegex(ValueError, 'changed after planning'):
            install.apply()
        self.assertEqual(path.read_text(), 'Concurrent operator change.\n')
        self.assertFalse((self.home / '.agents/ready-to-use-agent/policies/agent-setup.md').exists())

    def test_package_and_copy_installation_cannot_duplicate_compaction_hooks(self):
        path = self.home / '.pi/agent/settings.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'packages': ['git:github.com/AaronLiChen/ready-to-use-agent']}))
        with self.assertRaisesRegex(ValueError, 'one installation method'):
            self.install()
        self.assertFalse((self.home / '.pi/workflows/openai-native-compaction').exists())

    def test_cli_dry_run_is_usable_without_provider_login(self):
        result = subprocess.run([sys.executable, str(ROOT / 'install.py'), '--home', str(self.home), '--dry-run'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout))
        self.assertEqual(list(self.home.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
