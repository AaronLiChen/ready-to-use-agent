import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ready_agent_export', ROOT / 'scripts/check_export.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_clean_source_is_allowed(self):
        path = self.root / 'source.py'
        path.write_text('print("Portable configuration.")\n')
        self.assertEqual(exporter.findings(path, self.root), [])

    def test_token_values_are_rejected_without_returning_the_value(self):
        path = self.root / 'source.py'
        token = 'ghp_' + 'X' * 36
        path.write_text('TOKEN = ' + repr(token))
        found = exporter.findings(path, self.root)
        self.assertEqual(found, ['github-token'])
        self.assertNotIn(token, str(found))

    def test_auth_files_and_session_history_are_rejected(self):
        for name in ('auth.json', 'session.jsonl', 'models-store.json', '.env'):
            path = self.root / name
            path.write_text('PRIVATE_TEST_DATA')
            self.assertEqual(exporter.findings(path, self.root), ['private-state-file'])

    def test_binaries_and_symlinks_are_not_exported(self):
        path = self.root / 'binary.dat'
        path.write_bytes(b'\0\0')
        self.assertEqual(exporter.findings(path, self.root), ['binary-export'])
        link = self.root / 'source.py'
        try:
            link.symlink_to(path)
        except OSError:
            self.skipTest('This platform cannot create test symlinks')
        self.assertEqual(exporter.findings(link, self.root), ['symlink-export'])

    def test_git_scan_includes_forced_tracked_credential_files(self):
        subprocess.run(['git', '-C', str(self.root), 'init'], check=True, capture_output=True)
        path = self.root / 'auth.json'
        path.write_text('PRIVATE_TEST_DATA')
        subprocess.run(['git', '-C', str(self.root), 'add', 'auth.json'], check=True, capture_output=True)
        self.assertEqual(exporter.check(self.root), [('auth.json', 'private-state-file')])


if __name__ == '__main__':
    unittest.main()
