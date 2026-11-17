"""Explicit synthetic configuration for adapted controller regression tests."""
import functools
import os
from pathlib import Path
import sys
from unittest.mock import patch


def configured(setup):
    @functools.wraps(setup)
    def wrapped(self):
        setup(self)
        import workflow as w
        # Every adapted case sets ROOT to its private temporary directory first.
        if not str(w.ROOT).startswith(str(Path(self.temp.name))):
            raise AssertionError('Controller tests require isolated temporary state')
        config = w.ROOT / 'controller-config.json'
        if not config.exists():
            w.save(config, {'version': 1, 'background_enabled': True, 'backend': 'pi', 'github_login': 'Reviewer',
                            'delegation': {'comments': True, 'approvals': True}})
        account = getattr(self, 'config', {}).get('account', 'reviewer@example.test')
        defaults = {'ado': {'account': 'reviewer@example.test', 'organizations': ['example-org']},
                    'teams': {'account': account, 'self_id': 'my-aad-id', 'since': '2026-01-01T00:00:00Z',
                              'display_names': ['Example Reviewer'], 'ado_organizations': ['example-org'],
                              'github_owners': ['Azure'], 'reader_command': [sys.executable],
                              'reader_contract': {'version': 1, 'read_only': True, 'silent_only': True}}}
        for kind, values in defaults.items():
            path = w.ROOT / (kind + '-config.json')
            w.save(path, {**values, **(w.load(path) if path.exists() else {})})
        if isinstance(getattr(self, 'config', None), dict) and 'account' in self.config:
            self.config.setdefault('display_names', ['Example Reviewer'])
            self.config.setdefault('ado_organizations', ['example-org'])
            if self.__class__.__name__ == 'TeamsIntakeTests':
                self.config.update(reader_command=[sys.executable],
                                   reader_contract={'version': 1, 'read_only': True, 'silent_only': True})
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'AUTOWORKFLOW_ROOT': str(w.ROOT), 'AUTOWORKFLOW_JOB': '',
                                'AUTOWORKFLOW_INTAKE': '', 'AUTOWORKFLOW_AUTH_KIND': '',
                                'AUTOWORKFLOW_AUTH_ID': '', 'AUTOWORKFLOW_CYCLE': ''}).start()
    return wrapped
