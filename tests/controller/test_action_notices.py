import argparse
import copy
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "workflow"))
import workflow as w
import action_notices as n
import probes


class ActionNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(self.temp.name)
        patch.object(w, 'ROOT', self.root).start()
        patch.dict(os.environ, {'AUTOWORKFLOW_ROOT': str(self.root)}, clear=False).start()
        for key in ('AUTOWORKFLOW_JOB', 'AUTOWORKFLOW_INTAKE', 'PI_SESSION_ID', 'CLAUDE_SESSION_ID'):
            os.environ.pop(key, None)
        self.notify = patch.object(w, 'notify', return_value=True).start()
        w.save(self.root / 'action-notices-config.json', {'enabled': True, 'desktop': True, 'since': '2026-11-13T19:00:00Z'})
        w.save(self.root / 'auth/ado.json', {'expected_identity_id': 'self'})
        self.job = {'id': 'ado-notices', 'mode': 'review', 'intake': 'ado-notices', 'intake_source': 'ado',
                    'url': 'https://dev.azure.com/example-org/project/_git/repo/pullrequest/123',
                    'status': 'active', 'session': 'owner-session', 'owner': None,
                    'cycles': [], 'max_cycles_per_day': 12, 'cycle_timeout': 60,
                    'workspace': str(self.root), 'goal': 'Review the assigned PR', 'scope': 'Review only',
                    'acceptance': 'Current-head review and terminal state'}
        w.save(w.job_path(self.job['id']), self.job)
        w.save(self.root / 'inbox/ado-notices.json', {'source_kind': 'ado', 'self_id': 'self'})
        self.data = {'head': 'a' * 40, 'state': 'active', 'provider': 'ado', 'threads': [],
                     'author': {'id': 'author', 'displayName': 'Example Author'},
                     'reviewers': [{'id': 'self', 'vote': 0}], 'policies': []}
        self.args = argparse.Namespace(id=self.job['id'], kind='approval', head='a' * 40,
                                       thread_id=None, comment_id=None)
        w.save(self.root / 'snapshots/ado-notices.json', self.data)

    def events(self):
        return list(reversed(n.history(200)))

    def observe(self, data=None):
        return n.observe(self.job, self.data if data is None else data)

    def comment(self, author='self', text='A verified finding', number=1, **fields):
        return {'id': number, 'author': {'id': author, 'displayName': 'Example Reviewer' if author == 'self' else 'Example Author'},
                'commentType': 'text', 'content': text, 'publishedDate': '2026-11-13T20:00:00Z', **fields}

    def thread(self, comment=None, **fields):
        return {'id': 42, 'status': 'active', 'comments': [comment or self.comment()], **fields}

    def with_vote(self, vote):
        data = copy.deepcopy(self.data)
        data['reviewers'][0]['vote'] = vote
        return data

    def report(self, data=None):
        with patch.dict(os.environ, {'AUTOWORKFLOW_JOB': self.job['id']}), \
             patch.object(w, 'snapshot', return_value=data or self.with_vote(10)):
            return n.report(self.args)

    def test_examples_are_brief_and_use_verified_actor_names(self):
        self.assertEqual(n.message(self.job, 'first-review'), 'First review on PR 123')
        self.assertEqual(n.message(self.job, 'rereview', actor='Example Author'),
                         "Rereview PR 123 for Example Author's fixes/comments")
        self.assertEqual(n.message(self.job, 'approval'), 'Approved PR 123')
        self.assertEqual(n.message(self.job, 'approval', reapproval=True), 'Reapproved PR 123')
        for kind in ('preparation', 'first-review', 'rereview', 'validation', 'resume', 'comment',
                     'comment-updated', 'resolved', 'approval', 'merged', 'closed', 'blocked'):
            self.assertLessEqual(len(n.message(self.job, kind, actor='One Two Three Four Five').split()), 20)
        text = n.message(self.job, 'rereview', actor='Jose\n\x1b\u202e Blanquicet')
        self.assertNotIn('\n', text)
        self.assertNotIn('\x1b', text)
        self.assertNotIn('\u202e', text)

    def test_disabled_notices_do_not_write_or_notify(self):
        w.save(self.root / 'action-notices-config.json', {'enabled': False})
        self.assertIsNone(n.emit(self.job, 'first-review', 'event'))
        self.assertEqual(self.events(), [])
        self.notify.assert_not_called()

    def test_source_scope_includes_teams_but_not_unrequested_github_intake(self):
        self.assertTrue(n.enabled({**self.job, 'intake_source': 'teams'}))
        self.assertFalse(n.enabled({**self.job, 'intake_source': 'github'}))
        self.assertFalse(n.enabled({**self.job, 'mode': 'implement'}))
        request = {'id': 'teams-test', 'url': self.job['url']}
        self.assertTrue(n.enabled(request))

    def test_duplicate_sources_and_ado_url_aliases_share_one_notice(self):
        n.emit(self.job, 'first-review', 'same-event')
        alias = {**self.job, 'id': 'teams-alias', 'intake_source': 'teams',
                 'url': 'https://example-org.visualstudio.com/project-id/_git/repo-id/pullrequest/123'}
        result = n.emit(alias, 'first-review', 'same-event')
        self.assertFalse(result['new'])
        self.assertEqual(len(self.events()), 1)
        self.notify.assert_called_once_with('First review on PR 123')
        n.emit({**self.job, 'url': self.job['url'].replace('example-org/', 'otherorg/')}, 'first-review', 'same-event')
        self.assertEqual(len(self.events()), 2)

    def test_atomic_deduplication_across_real_processes(self):
        w.save(self.root / 'action-notices-config.json', {'enabled': True, 'desktop': False})
        code = "import json,sys; import action_notices as n; print(json.dumps(n.emit(json.loads(sys.stdin.read()),'first-review','same-event')))"
        env = {**os.environ, 'PYTHONPATH': str(w.CODE)}
        processes = [subprocess.Popen([sys.executable, '-c', code], env=env, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(6)]
        outputs = [p.communicate(json.dumps(self.job), timeout=15) for p in processes]
        for p, (_, error) in zip(processes, outputs):
            self.assertEqual(p.returncode, 0, error)
        self.assertEqual(sum(json.loads(out)['new'] for out, _ in outputs), 1)
        self.assertEqual(len(self.events()), 1)

    def test_history_has_no_network_or_model_calls(self):
        n.emit(self.job, 'first-review', 'event')
        with patch.object(sys, 'argv', ['agent-workflow', 'activity', '--limit', '1']), \
             patch.object(w, 'snapshot') as provider, patch.object(w, 'run_worker') as worker, \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            w.main()
        self.assertIn('First review on PR 123', output.getvalue())
        provider.assert_not_called()
        worker.assert_not_called()
        with self.assertRaises(ValueError):
            n.history(10000)

    def test_baseline_does_not_replay_old_posts_or_approvals(self):
        self.data['threads'] = [self.thread()]
        self.data['reviewers'][0]['vote'] = 10
        self.observe()
        self.observe()
        self.assertEqual(self.events(), [])
        self.notify.assert_not_called()

    def test_unchanged_polls_remain_quiet(self):
        self.observe()
        for _ in range(10):
            self.observe()
        self.assertEqual(self.events(), [])
        self.notify.assert_not_called()

    def test_new_own_comment_is_announced_once_without_its_text(self):
        self.observe()
        self.data['threads'] = [self.thread(self.comment(text='PRIVATE_TEAMS_TEXT token-like secret'))]
        self.observe()
        self.observe()
        self.assertEqual([e['text'] for e in self.events()], ['Posted a review comment on PR 123'])
        persisted = ''.join(p.read_text() for p in (self.root / 'action-notices').rglob('*.json'))
        self.assertNotIn('PRIVATE_TEAMS_TEXT', persisted)
        self.assertNotIn('token-like secret', persisted)

    def test_own_comment_edit_is_not_called_a_new_post(self):
        self.data['threads'] = [self.thread()]
        self.observe()
        self.data['threads'][0]['comments'][0]['content'] = 'An updated finding'
        self.observe()
        self.assertEqual(self.events()[0]['text'], 'Updated a review comment on PR 123')

    def test_old_comment_from_a_stale_baseline_is_not_replayed(self):
        self.observe()
        comment = self.comment(publishedDate='2026-11-11T08:15:00Z')
        self.data['threads'] = [self.thread(comment)]
        self.observe()
        self.assertEqual(self.events(), [])

    def test_other_authors_comments_only_supply_rereview_context(self):
        self.observe()
        self.data['threads'] = [self.thread(self.comment(author='author', text='PRIVATE reply body'))]
        self.observe()
        self.assertEqual(self.events(), [])
        job = {**self.job, 'last_worker_log': '/private/previous-cycle.log', 'attention_epoch': 'reply-event'}
        n.review_started(job, self.data)
        self.assertEqual(self.events()[0]['text'], "Rereview PR 123 for Example Author's fixes/comments")
        self.assertNotIn('PRIVATE reply body', json.dumps(self.events()))

    def test_new_head_starts_rereview_without_inventing_a_person(self):
        self.observe()
        self.data['head'] = 'b' * 40
        self.observe()
        job = {**self.job, 'last_worker_log': '/previous.log', 'attention_epoch': 'new-source'}
        n.review_started(job, self.data)
        self.assertEqual(self.events()[0]['text'], 'Rereview PR 123 after new commits or comments')

    def test_ci_only_work_is_not_reported_as_a_code_fix(self):
        self.observe()
        self.data['policies'] = [{'state': 'approved'}]
        self.observe()
        n.review_started({**self.job, 'last_worker_log': '/previous.log', 'attention_epoch': 'ci-event'}, self.data)
        self.assertEqual(self.events()[0]['text'], 'Review CI results for PR 123')

    def test_start_and_resume_notices_are_deduplicated(self):
        self.job['attention_epoch'] = 'event'
        n.review_started(self.job, self.data)
        n.review_started(self.job, self.data)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]['text'], 'First review on PR 123')
        resumed = {**self.job, 'last_worker_log': '/previous.log', 'pending_action': 'comment'}
        n.review_started(resumed, self.data)
        n.review_started(resumed, self.data)
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(self.events()[-1]['text'], 'Resume PR 123 to post a comment')

    def test_preparation_does_not_claim_substantive_review(self):
        request = {**self.job, 'source_kind': 'ado'}
        n.preparation_started(request)
        n.preparation_started(request)
        self.assertEqual([e['text'] for e in self.events()], ['Prepare review context for PR 123'])

    def test_green_ci_without_a_vote_cannot_create_an_approval_notice(self):
        self.observe()
        self.data['policies'] = [{'blocking': True, 'state': 'approved'}]
        self.observe()
        self.assertEqual(self.events(), [])
        with self.assertRaisesRegex(ValueError, 'does not verify'):
            self.report(self.data)

    def test_approval_is_announced_only_after_a_positive_own_vote(self):
        self.observe()
        approved = self.with_vote(10)
        self.observe(approved)
        self.observe(approved)
        self.assertEqual([e['text'] for e in self.events()], ['Approved PR 123'])

    def test_retained_vote_on_a_new_head_is_not_a_new_approval(self):
        approved = self.with_vote(10)
        self.observe(approved)
        approved['head'] = 'b' * 40
        self.observe(approved)
        self.assertEqual(self.events(), [])

    def test_report_and_probe_share_approval_deduplication(self):
        self.observe()
        self.report()
        self.report()
        self.observe(self.with_vote(10))
        self.assertEqual([e['text'] for e in self.events()], ['Approved PR 123'])

    def test_reapproval_after_a_new_head_requires_an_explicit_verified_report(self):
        approved = self.with_vote(10)
        self.observe(approved)
        approved['head'] = 'b' * 40
        self.observe(approved)
        self.args.head = 'b' * 40
        self.report(approved)
        self.report(approved)
        self.assertEqual([e['text'] for e in self.events()], ['Reapproved PR 123'])

    def test_reset_vote_can_be_reapproved_on_the_same_head(self):
        self.observe()
        self.observe(self.with_vote(10))
        self.observe(self.with_vote(0))
        self.observe(self.with_vote(10))
        self.assertEqual([e['text'] for e in self.events()], ['Approved PR 123', 'Reapproved PR 123'])

    def test_self_authored_pr_is_not_announced_as_approved(self):
        self.data['author']['id'] = 'self'
        self.observe()
        self.observe(self.with_vote(10))
        self.assertEqual(self.events(), [])
        with self.assertRaisesRegex(ValueError, 'does not verify'):
            self.report(self.with_vote(10))

    def test_identity_mismatch_cannot_verify_a_completed_action(self):
        w.save(self.root / 'auth/ado.json', {'expected_identity_id': 'someone-else'})
        with self.assertRaisesRegex(ValueError, 'does not verify'):
            self.report()
        self.assertEqual(self.events(), [])

    def test_foreign_workers_and_preparation_cannot_report_review_actions(self):
        for environment in ({'AUTOWORKFLOW_JOB': 'other-job'}, {'AUTOWORKFLOW_INTAKE': self.job['id']}, {}):
            with patch.dict(os.environ, environment), patch.object(w, 'snapshot') as provider:
                with self.assertRaises(ValueError):
                    n.report(self.args)
                provider.assert_not_called()
        self.assertEqual(self.events(), [])

    def test_a_stale_head_cannot_be_announced_as_approved(self):
        approved = self.with_vote(10)
        approved['head'] = 'b' * 40
        with self.assertRaisesRegex(ValueError, 'does not verify'):
            self.report(approved)
        self.assertEqual(self.events(), [])

    def test_failed_live_verification_defers_notice_without_repeating_the_pr_write(self):
        with patch.dict(os.environ, {'AUTOWORKFLOW_JOB': self.job['id']}), \
             patch.object(w, 'snapshot', side_effect=RuntimeError('Bearer PRIVATE_SECRET')):
            result = n.report(self.args)
        self.assertEqual(result['state'], 'awaiting_verification')
        self.assertEqual(self.events(), [])
        pending = w.load(n.state_path(self.job))
        self.assertEqual(len(pending['pending']), 1)
        self.assertNotIn('PRIVATE_SECRET', json.dumps(pending))
        self.observe(self.with_vote(10))
        self.assertEqual([e['text'] for e in self.events()], ['Approved PR 123'])
        self.assertEqual(w.load(n.state_path(self.job))['pending'], [])

    def test_comment_receipt_requires_a_visible_own_comment(self):
        self.args.kind = 'comment'
        self.args.thread_id = '42'
        self.args.comment_id = '1'
        with self.assertRaisesRegex(ValueError, 'does not verify'):
            self.report(self.data)
        self.data['threads'] = [self.thread(self.comment(author='author'))]
        with self.assertRaisesRegex(ValueError, 'does not verify'):
            self.report(self.data)
        self.data['threads'] = [self.thread()]
        self.report(self.data)
        self.report(self.data)
        self.assertEqual([e['text'] for e in self.events()], ['Posted a review comment on PR 123'])

    def test_comment_receipt_requests_threads_even_if_the_pr_just_merged(self):
        self.args.kind, self.args.thread_id, self.args.comment_id = 'comment', '42', '1'
        self.data.update(state='completed', threads=[self.thread()])
        with patch.dict(os.environ, {'AUTOWORKFLOW_JOB': self.job['id']}), \
             patch.object(w, 'snapshot', return_value=self.data) as provider:
            n.report(self.args)
        self.assertTrue(provider.call_args[0][0]['include_terminal_threads'])
        self.assertIn('Posted a review comment on PR 123', [e['text'] for e in self.events()])

    def test_deleted_or_system_comments_do_not_verify_a_post(self):
        self.args.kind, self.args.thread_id, self.args.comment_id = 'comment', '42', '1'
        for fields in ({'isDeleted': True}, {'commentType': 'system'}):
            self.data['threads'] = [self.thread(self.comment(**fields))]
            with self.assertRaisesRegex(ValueError, 'does not verify'):
                self.report(self.data)
        self.assertEqual(self.events(), [])

    def test_resolution_does_not_claim_a_verified_code_fix(self):
        self.data['threads'] = [self.thread()]
        self.observe()
        self.data['threads'][0]['status'] = 'fixed'
        self.observe()
        self.observe()
        self.assertEqual([e['text'] for e in self.events()], ['Review comment marked resolved on PR 123'])

    def test_terminal_notices_distinguish_merged_and_abandoned(self):
        for terminal, message in (('completed', 'PR 123 merged'), ('abandoned', 'PR 123 closed without merge')):
            with self.subTest(terminal=terminal):
                path = n.state_path(self.job)
                path.unlink(missing_ok=True)
                self.observe()
                self.observe({**self.data, 'state': terminal})
                self.observe({**self.data, 'state': terminal})
                self.assertIn(message, [e['text'] for e in self.events()])
        self.assertEqual(len(self.events()), 2)

    def test_github_teams_notices_use_the_pinned_login_and_exact_review_head(self):
        self.job.update(intake_source='teams', github_login='reviewer', github_user_id=123,
                        url='https://github.com/Azure/repo/pull/42')
        data = {'head': 'a' * 40, 'state': 'open', 'author': 'other', 'threads': [], 'reviews': []}
        n.observe(self.job, data)
        data['reviews'] = [{'id': 12, 'author': 'reviewer', 'state': 'APPROVED',
                            'commit_id': 'b' * 40, 'submitted_at': '2026-11-13T20:00:00Z'}]
        n.observe(self.job, data)
        self.assertEqual(self.events(), [])
        data['reviews'][0]['commit_id'] = 'a' * 40
        n.observe(self.job, data)
        self.assertEqual(self.events()[0]['text'], 'Approved PR azure/repo#42')

    def test_completed_ado_comment_receipt_fetches_threads_without_ci_reads(self):
        requests = []
        pr = {'status': 'completed', 'sourceRefName': 'refs/heads/feature', 'targetRefName': 'refs/heads/main',
              'lastMergeSourceCommit': {'commitId': 'a' * 40}, 'repository': {'id': 'repo-id'}, 'reviewers': []}

        class Response:
            def __init__(self, value):
                self.stream, self.headers = io.StringIO(json.dumps(value)), {}
            def __enter__(self):
                return self
            def __exit__(self, *_):
                self.stream.close()
            def read(self):
                return self.stream.read()

        def fetch(request, timeout):
            requests.append(request.full_url)
            if '/threads?' in request.full_url:
                return Response({'value': [self.thread()]})
            return Response(pr)

        with patch.object(probes, 'acquire_ado_token', return_value='TEST_ONLY'), \
             patch.object(probes.urllib.request, 'build_opener', return_value=SimpleNamespace(open=fetch)):
            data = probes.ado_snapshot({**self.job, 'include_terminal_threads': True})
            self.assertEqual(len(data['threads']), 1)
            self.assertEqual(len(requests), 2)
            requests.clear()
            data = probes.ado_snapshot(self.job)
            self.assertNotIn('threads', data)
            self.assertEqual(len(requests), 1)
        self.assertNotIn('TEST_ONLY', json.dumps(data))

    def test_notification_failure_is_durable_and_does_not_change_the_job(self):
        self.notify.side_effect = OSError('PRIVATE_SECRET')
        before = w.load(w.job_path(self.job['id']))
        n.safe(n.emit, self.job, 'first-review', 'event')
        self.assertEqual(w.load(w.job_path(self.job['id'])), before)
        self.assertFalse(self.events()[0]['notification_request_accepted'])
        notice = w.load(self.root / 'attention/action-notices.json')
        self.assertNotIn('PRIVATE_SECRET', json.dumps(notice))
        n.safe(n.emit, self.job, 'first-review', 'event')
        self.notify.assert_called_once()

    def test_incomplete_snapshots_do_not_create_completed_action_notices(self):
        for data in ({'head': 'a' * 40}, {'state': 'completed'}):
            self.assertFalse(self.observe(data))
        self.assertEqual(self.events(), [])

    def test_actual_subprocess_start_and_handoff_emit_one_first_review_notice(self):
        script = self.root / 'fake-pi'
        cli = str(Path(w.__file__).resolve())
        script.write_text('#!' + sys.executable + '\nimport os,subprocess,sys\n'
                          "prompt=sys.stdin.read()\nassert 'agent-workflow action' in prompt\n"
                          f"subprocess.check_call([sys.executable,{cli!r},'checkpoint',os.environ['AUTOWORKFLOW_JOB'],'--status','waiting','--next','Watch replies','--evidence','Test-only handoff'])\n")
        script.chmod(0o700)
        with patch.object(w, 'PI', script):
            self.assertTrue(w.run_worker(self.job, self.root / 'snapshots/ado-notices.json'))
        self.assertEqual([e['text'] for e in self.events()], ['First review on PR 123'])
        job = w.load(w.job_path(self.job['id']))
        self.assertEqual(job['status'], 'waiting')
        self.assertEqual(job['last_worker_exit'], 0)

    def test_failed_process_launch_does_not_claim_a_review_started(self):
        with patch.object(w, 'PI', self.root / 'does-not-exist'):
            with self.assertRaises(FileNotFoundError):
                w.run_worker(self.job, self.root / 'snapshots/ado-notices.json')
        self.assertEqual(self.events(), [])


from controller.fixtures import configured
ActionNoticeTests.setUp = configured(ActionNoticeTests.setUp)

if __name__ == '__main__':
    unittest.main()
