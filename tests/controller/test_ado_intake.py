import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,MagicMock
import urllib.parse

import workflow as w
import ado_intake as a
import teams_intake


class ADOIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        self.root=Path(self.temp.name);patch.object(w,'ROOT',self.root).start();patch.object(w,'notify').start()
        w.save(w.ROOT/'scheduler-config.json',{'global_cycles_per_day':48})
        self.config={'enabled':True,'auto_start_reviews':True,'account':'reviewer@example.test','organizations':['example-org']}
        self.identity={'id':'reviewer-id','account':self.config['account']}
        w.save(a.config_path(),self.config)

    def pr(self,**changes):
        data={'pullRequestId':42,'status':'active','isDraft':False,'createdBy':{'id':'other','uniqueName':'other@example.invalid'},
              'lastMergeSourceCommit':{'commitId':'a'*40},'reviewers':[{'id':'reviewer-id','vote':0,'isRequired':False}],
              'repository':{'id':'repo-id','name':'repo','project':{'id':'project-id','name':'project'}}}
        data.update(changes);return data

    def record(self,**changes):return a.record(self.config,'example-org',self.identity,self.pr(**changes))

    def client(self,pr=None):
        client=MagicMock();client.identity.return_value=self.identity;client.get.return_value=(pr or self.pr(),None)
        patch.object(a,'ADOClient',return_value=client).start();return client

    def test_auth_warning_lines_are_not_sent_as_header_content(self):
        import probes
        self.assertEqual(probes.clean_ado_token('notice from CLI\nheader.payload.signature\n'),'header.payload.signature')
        self.assertEqual(probes.clean_ado_token('opaque-single-line-token\n'),'opaque-single-line-token')
        with self.assertRaises(RuntimeError):probes.clean_ado_token('a.b.c\nx.y.z')
        with self.assertRaises(RuntimeError):probes.clean_ado_token('notice\nnot-a-token')

    def test_optional_and_required_assignments_are_both_eligible(self):
        for required in [False,True]:
            record=self.record(reviewers=[{'id':'reviewer-id','isRequired':required,'vote':0}])
            self.assertEqual(record['status'],'pending');self.assertEqual(record['required'],required)

    def test_no_direct_assignment_is_not_implicitly_a_group_assignment(self):
        self.assertIsNone(self.record(reviewers=[{'id':'some-team','isContainer':True}]))
        self.assertEqual(a.inbox(),[])

    def test_drafts_and_self_authored_prs_do_not_start_review(self):
        self.assertEqual(self.record(isDraft=True)['status'],'draft_hold')
        self.assertEqual(self.record(createdBy={'id':'reviewer-id'})['status'],'self_authored')

    def test_unknown_draft_state_is_held(self):
        self.assertEqual(self.record(isDraft=None)['status'],'draft_hold')

    def test_existing_job_deduplicates_project_and_repo_id_aliases(self):
        pr=self.pr();w.save(w.job_path('existing'),{'id':'existing','url':a.aliases('example-org',pr)[-1],'status':'waiting'})
        entry=self.record();self.assertEqual(entry['status'],'linked');self.assertEqual(entry['job'],'existing')

    def test_legacy_watcher_keeps_its_original_authority(self):
        with patch.object(w,'legacy_watcher',return_value='/existing/watch'):
            self.assertEqual(self.record()['status'],'existing_watch')

    def test_live_verification_rejects_removed_draft_self_and_wrong_identity(self):
        request=self.record();client=self.client()
        self.assertTrue(a.verify_request(self.config,request))
        for pr in [self.pr(reviewers=[]),self.pr(isDraft=True),self.pr(createdBy={'id':'reviewer-id'}),self.pr(status='completed')]:
            client.get.return_value=(pr,None)
            self.assertFalse(a.verify_request(self.config,request))
        client.identity.return_value={'id':'different'}
        self.assertFalse(a.verify_request(self.config,request))

    def test_wrong_organization_is_rejected_without_fetch(self):
        request=self.record();request['assignment']['organization']='other-org'
        with patch.object(a,'ADOClient') as client:
            self.assertFalse(a.verify_request(self.config,request));client.assert_not_called()

    def test_disabled_poll_does_not_authenticate_or_read_ado(self):
        w.save(a.config_path(),{**self.config,'enabled':False})
        with patch.object(a,'ADOClient') as client:a.poll();client.assert_not_called()

    def test_successful_empty_scan_withdraws_pending_not_existing_watch(self):
        entry=self.record();client=self.client();client.assigned.return_value=[]
        a.poll();self.assertEqual(a.inbox()[0]['status'],'withdrawn')
        self.assertTrue(a.coverage_summary()['organizations']['example-org']['complete'])

    def test_completed_pr_does_not_leave_failed_preparation_blocked_forever(self):
        entry=self.record()
        teams_intake.update_request(entry['id'],{'status':'blocked','decision':'Provider connection failed'})
        client=self.client();client.assigned.return_value=[]
        client.get.return_value=(self.pr(status='completed'),None)
        a.poll()
        self.assertEqual(a.inbox()[0]['status'],'terminal')
        self.assertEqual(a.inbox()[0]['terminal_state'],'completed')
        self.assertEqual(w.jobs(),[])

    def test_failed_scan_is_not_treated_as_an_empty_scan(self):
        self.record();client=self.client();client.assigned.side_effect=RuntimeError('incomplete')
        a.poll();self.assertEqual(a.inbox()[0]['status'],'pending')
        self.assertFalse(a.coverage_summary()['organizations']['example-org']['complete'])
        self.assertNotIn('last_success_at',a.coverage_summary())

    def test_preparation_deduplicates_before_model_or_api_call(self):
        entry=self.record();w.save(w.job_path('existing'),{'id':'existing','url':entry['url'],'status':'waiting'})
        with patch.object(a,'verify_request') as verify,patch.object(teams_intake,'prepare_request') as model:
            self.assertFalse(a.prepare_one());verify.assert_not_called();model.assert_not_called()
        self.assertEqual(a.inbox()[0]['status'],'linked')

    def test_preparation_reverifies_before_shared_worker(self):
        self.record()
        with patch.object(a,'verify_request',return_value=False),patch.object(teams_intake,'prepare_request') as model:
            self.assertFalse(a.prepare_one());model.assert_not_called()
        self.assertEqual(a.inbox()[0]['status'],'withdrawn_or_not_ready')

    def test_numeric_pagination_reads_past_one_hundred(self):
        client=a.ADOClient.__new__(a.ADOClient);calls=[]
        def get(path):
            calls.append(path);skip=int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get('$skip',['0'])[0])
            return ({'value':[{'pullRequestId':i} for i in range(skip,min(skip+100,101))]},None)
        client.get=get
        self.assertEqual(len(client.assigned('reviewer-id')),101)
        self.assertEqual(len(calls),2)

    def test_repeated_page_is_an_error_not_complete_coverage(self):
        client=a.ADOClient.__new__(a.ADOClient);client.get=lambda path:({'value':[{'pullRequestId':i} for i in range(100)]},None)
        with self.assertRaisesRegex(RuntimeError,'did not advance'):client.assigned('reviewer-id')

    def test_sources_share_the_existing_preparation_budget(self):
        self.record();w.save(w.ROOT/'teams-worker-budget.json',{'cycles':[__import__('time').time()]*48})
        with patch.object(a,'verify_request') as verify:
            self.assertFalse(a.prepare_one());verify.assert_not_called()

    def test_source_rotation_prevents_ado_from_starving_teams(self):
        import teams_intake
        w.save(w.ROOT/'intake-scheduler-state.json',{'last_source':'teams'})
        with patch.object(a,'prepare_one',return_value=True) as ado,patch.object(teams_intake,'prepare_one') as teams:
            self.assertTrue(w.prepare_intake());ado.assert_called_once();teams.assert_not_called()

    def test_unknown_source_is_not_silently_trusted(self):
        with self.assertRaises(ValueError):w.intake_adapter({'source_kind':'untrusted'})


from controller.fixtures import configured
ADOIntakeTests.setUp = configured(ADOIntakeTests.setUp)

if __name__=='__main__':unittest.main()
