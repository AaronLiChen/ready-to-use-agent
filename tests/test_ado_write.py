"""Verify ADO write scope with synthetic identities, receipts, and provider responses."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_install import ROOT
sys.path.insert(0, str(ROOT / 'workflow'))
import workflow as w
import ado_write


class ADOWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        self.home = Path(self.temp.name)
        patch.object(w, 'ROOT', self.home / 'state').start()
        patch.object(w, 'notify').start()
        self.job = {'id': 'fixture', 'url': 'https://dev.azure.com/example-org/Project/_git/repo/pullrequest/42',
                    'mode': 'review', 'expected_target': 'refs/heads/main'}
        patch.object(ado_write.write_guard, 'assigned', return_value=self.job).start()
        self.authorize = patch.object(ado_write.write_guard, 'authorize', return_value={}).start()
        self.acquire = patch.object(ado_write.auth_recovery, 'acquire', return_value='synthetic-token').start()
        patch.object(ado_write.auth_recovery, 'expected_account', return_value='reviewer@example.test').start()
        self.identity = patch.object(ado_write.ado, '_get', return_value={
            'authenticatedUser': {'id': 'fixture-reviewer', 'properties': {'Account': {'$value': 'reviewer@example.test'}}}}).start()
        self.current = patch.object(ado_write.ado, 'api_get_pr', return_value={
            'status': 'active', 'isDraft': False, 'targetRefName': 'refs/heads/main',
            'lastMergeSourceCommit': {'commitId': 'a' * 40}}).start()
        self.send = patch.object(ado_write, 'send', return_value={'id': 8, 'comments': [{'id': 1}]}).start()
        self.body = self.home / 'comment.txt'
        self.body.write_text('Verified source finding.\n')

    def args(self, **changes):
        return SimpleNamespace(**{'head': 'a' * 40, 'approve': False, 'comment_file': str(self.body),
                                 'reply_thread': None, 'thread_context': None, **changes})

    def test_new_thread_uses_only_the_enrolled_pr(self):
        result = ado_write.execute(self.args())
        self.assertEqual(result, {'head': 'a' * 40, 'thread_id': 8, 'comment_id': 1})
        url, token, method, data = self.send.call_args.args
        self.assertEqual(url, 'https://dev.azure.com/example-org/Project/_apis/git/repositories/repo/pullRequests/42/threads?api-version=7.1')
        self.assertEqual(method, 'POST')
        self.assertEqual(data['comments'][0]['content'], self.body.read_text())
        self.acquire.assert_called_once_with(ado_write.ado, force=True)

    def test_reply_uses_the_explicit_existing_thread(self):
        self.send.return_value = {'id': 3}
        result = ado_write.execute(self.args(reply_thread=8))
        self.assertEqual(result['thread_id'], 8)
        self.assertEqual(result['comment_id'], 3)
        self.assertTrue(self.send.call_args.args[0].endswith('/threads/8/comments?api-version=7.1'))

    def test_approval_uses_the_verified_operator_identity(self):
        self.send.return_value = {'id': 'fixture-reviewer', 'vote': 10}
        result = ado_write.execute(self.args(approve=True, comment_file=None))
        self.authorize.assert_called_once_with('approval', 'a' * 40, self.job)
        self.assertEqual(self.send.call_args.args[2:], ('PUT', {'vote': 10}))
        self.assertIn('/reviewers/fixture-reviewer?', self.send.call_args.args[0])
        self.assertEqual(result['vote'], 10)

    def test_guard_failure_precedes_token_retrieval(self):
        self.authorize.side_effect = ValueError('No operator delegation')
        with self.assertRaisesRegex(ValueError, 'delegation'):
            ado_write.execute(self.args())
        self.acquire.assert_not_called()
        self.send.assert_not_called()

    def test_source_target_or_draft_change_prevents_approval(self):
        original = self.current.return_value
        for change in ({'lastMergeSourceCommit': {'commitId': 'b' * 40}},
                       {'targetRefName': 'refs/heads/other'}, {'isDraft': True}, {'status': 'completed'}):
            with self.subTest(change=change):
                self.current.return_value = {**original, **change}
                with self.assertRaisesRegex(ValueError, 'changed'):
                    ado_write.execute(self.args(approve=True, comment_file=None))
        self.send.assert_not_called()

    def test_wrong_identity_cannot_write(self):
        self.identity.return_value['authenticatedUser']['properties']['Account']['$value'] = 'other@example.test'
        with self.assertRaises(ado_write.auth_recovery.AuthError):
            ado_write.execute(self.args())
        self.send.assert_not_called()

    def test_inline_context_cannot_supply_unrelated_request_fields(self):
        path = self.home / 'context.json'
        path.write_text(json.dumps({'filePath': '/src/example.py', 'reviewerId': 'other'}))
        with self.assertRaisesRegex(ValueError, 'context'):
            ado_write.execute(self.args(thread_context=str(path)))
        self.acquire.assert_not_called()
        self.send.assert_not_called()

    def test_missing_receipt_is_not_success_or_an_automatic_retry(self):
        self.send.return_value = {}
        with self.assertRaisesRegex(RuntimeError, 'uncertain'):
            ado_write.execute(self.args())
        self.assertEqual(self.send.call_count, 1)


if __name__ == '__main__':
    unittest.main()
