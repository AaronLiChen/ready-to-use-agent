import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch,MagicMock

import workflow as w
import github_intake as g
import github_auth as auth
import teams_intake
import parallel_dispatch


class GitHubIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.addCleanup(patch.stopall)
        self.root=Path(self.temp.name);patch.object(w,'ROOT',self.root).start();patch.object(w,'notify',return_value=True).start()
        for key in ['AUTOWORKFLOW_JOB','AUTOWORKFLOW_INTAKE','AUTOWORKFLOW_AUTH_KIND','AUTOWORKFLOW_AUTH_ID','AUTOWORKFLOW_GITHUB_LOGIN']:patch.dict(os.environ,{key:''}).start()
        self.config={'enabled':True,'auto_start_reviews':True,'logins':['Reviewer','Reviewer_work']}
        w.save(g.config_path(),self.config)
        self.client=SimpleNamespace(login='Reviewer',identity={'login':'Reviewer','id':123})

    def pr(self,**changes):
        value={'html_url':'https://github.com/Azure/repo/pull/42','state':'open','draft':False,
               'head':{'sha':'a'*40},'user':{'login':'author'},'requested_reviewers':[{'login':'Reviewer'}]}
        value.update(changes);return value

    def record(self,**changes):return g.record(self.config,self.client,self.pr(**changes))

    def test_all_direct_requests_qualify_without_required_filter(self):
        row=self.record();self.assertEqual(row['status'],'pending')
        self.assertIn('all direct requests',row['assignment']['required_or_optional'])

    def test_group_only_requests_do_not_become_individual_assignments(self):
        self.assertIsNone(self.record(requested_reviewers=[],requested_teams=[{'slug':'team'}]))

    def test_drafts_and_either_own_account_are_held(self):
        self.assertEqual(self.record(draft=True)['status'],'draft_hold')
        self.assertEqual(self.record(user={'login':'Reviewer_work'})['status'],'self_authored')

    def test_teams_watch_and_case_alias_are_deduplicated(self):
        w.save(w.job_path('teams-existing'),{'id':'teams-existing','url':'https://github.com/azure/REPO/pull/42','status':'waiting'})
        row=self.record();self.assertEqual(row['status'],'linked');self.assertEqual(row['job'],'teams-existing')

    def test_two_accounts_do_not_manufacture_two_votes_or_reviews(self):
        row=self.record()
        second=SimpleNamespace(login='Reviewer_work',identity={'login':'Reviewer_work','id':456})
        again=g.record(self.config,second,self.pr(requested_reviewers=[{'login':'Reviewer_work'}]))
        self.assertEqual(len(g.inbox()),1);self.assertEqual(again['github_login'],'Reviewer')
        self.assertEqual(set(again['requested_logins']),{'Reviewer','Reviewer_work'})

    def test_verification_rejects_removed_request_self_draft_and_wrong_identity(self):
        row=self.record();client=MagicMock();client.identity=self.client.identity;client.get.return_value=self.pr()
        with patch.object(g,'GitHubClient',return_value=client):
            self.assertTrue(g.verify_request(self.config,row))
            for pr in [self.pr(requested_reviewers=[]),self.pr(draft=True),self.pr(user={'login':'Reviewer_work'}),self.pr(state='closed')]:
                client.get.return_value=pr;self.assertFalse(g.verify_request(self.config,row))
            client.identity={'id':999,'login':'Reviewer'}
            self.assertFalse(g.verify_request(self.config,row))

    def test_request_consumed_after_review_does_not_end_existing_watch(self):
        w.save(w.job_path('existing'),{'id':'existing','url':self.pr()['html_url'],'status':'waiting'})
        self.record();client=MagicMock();client.identity=self.client.identity;client.requested.return_value=[]
        with patch.object(g,'GitHubClient',return_value=client):g.poll()
        self.assertEqual(g.inbox()[0]['status'],'linked')
        self.assertEqual(w.load(w.job_path('existing'))['status'],'waiting')

    def test_reopened_pr_can_get_a_new_watch_without_overwriting_closed_history(self):
        first=self.record();w.save(w.job_path(first['id']),{'id':first['id'],'url':first['url'],'status':'done','intake':first['id']})
        next_request=self.record()
        self.assertNotEqual(first['id'],next_request['id']);self.assertTrue(next_request['id'].endswith('-r1'))
        self.assertEqual(next_request['status'],'pending')

    def test_incomplete_search_is_not_reported_as_empty_success(self):
        client=g.GitHubClient.__new__(g.GitHubClient);client.login='Reviewer'
        client.get=lambda path:{'incomplete_results':True,'total_count':1,'items':[]}
        with self.assertRaises(RuntimeError):client.requested()
        client.get=lambda path:{'incomplete_results':False,'total_count':1001,'items':[]}
        with self.assertRaises(RuntimeError):client.requested()

    def test_search_paginates_beyond_first_hundred(self):
        client=g.GitHubClient.__new__(g.GitHubClient);client.login='Reviewer';calls=[]
        def get(path):
            import urllib.parse
            calls.append(path);page=int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)['page'][0])
            return {'total_count':101,'items':[{'id':i} for i in (range(100) if page==1 else [100])]}
        client.get=get;self.assertEqual(len(client.requested()),101);self.assertEqual(len(calls),2)

    def test_disabled_intake_is_network_free(self):
        w.save(g.config_path(),{**self.config,'enabled':False})
        with patch.object(g,'GitHubClient') as client:g.poll();client.assert_not_called()

    def test_prepare_deduplicates_before_model_and_live_fetch(self):
        row=self.record();w.save(w.job_path('existing'),{'id':'existing','url':row['url'],'status':'waiting'})
        with patch.object(g,'verify_request') as verify,patch.object(teams_intake,'prepare_request') as worker:
            self.assertFalse(g.prepare_one());verify.assert_not_called();worker.assert_not_called()

    def test_token_is_child_scoped_and_never_switches_global_account(self):
        with patch.object(auth,'native',return_value='/fixture/gh'),patch.object(subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='FIXTURE_TOKEN\n')) as run:
            env=auth.token_env('Reviewer',{'PATH':'/usr/bin'})
        self.assertEqual(env['GH_TOKEN'],'FIXTURE_TOKEN')
        self.assertEqual(run.call_args.args[0],['/fixture/gh','auth','token','--hostname','github.com','--user','Reviewer'])
        self.assertNotIn('GH_TOKEN',run.call_args.kwargs['env'])
        self.assertNotIn('FIXTURE_TOKEN',''.join(p.read_text() for p in self.root.rglob('*.json')))

    def test_restored_github_identity_resumes_waiting_action_without_new_head(self):
        row=self.record();w.save(w.job_path('review-one'),{'id':'review-one','url':row['url'],'status':'waiting','github_login':'Reviewer','owner':None})
        with patch.dict(os.environ,{'AUTOWORKFLOW_JOB':'review-one'}):auth.failure('Reviewer')
        self.assertFalse(auth.may_run('job','review-one'))
        auth.verified('Reviewer',{'login':'Reviewer','id':123})
        self.assertEqual(w.load(w.job_path('review-one'))['status'],'ready')

    def test_identity_change_and_unrelated_blockers_are_not_waived(self):
        auth.verified('Reviewer',{'login':'Reviewer','id':123})
        with self.assertRaises(auth.GitHubAuthError):auth.verified('Reviewer',{'login':'Reviewer','id':456})

    def test_github_requests_are_visible_to_parallel_planner(self):
        self.record()
        rows=parallel_dispatch.candidates()
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['kind'],'intake')

    def repository_config(self,entries=None):
        self.config={**self.config,'repository_watches':entries if entries is not None else [
            {'repository':'Azure/repo','login':'Reviewer','enabled':True}]}
        w.save(g.config_path(),self.config)
        return self.config

    def collector(self,prs=None,requested=None):
        client=MagicMock();client.login='Reviewer';client.identity=self.client.identity
        client.requested.return_value=[] if requested is None else requested
        client.repository_prs.return_value=[self.pr(requested_reviewers=[])] if prs is None else prs
        client.get.return_value=self.pr(requested_reviewers=[])
        return client

    def test_repository_watch_accepts_an_unassigned_pr_only_in_its_exact_scope(self):
        self.repository_config()
        row=self.record(requested_reviewers=[])
        self.assertEqual(row['status'],'pending')
        self.assertEqual(row['assignment']['repository_watch'],'Azure/repo')
        self.assertFalse(row['assignment']['direct_request'])
        self.assertIsNone(self.record(html_url='https://github.com/Azure/other/pull/42',requested_reviewers=[]))

    def test_repository_watch_uses_one_pinned_account(self):
        self.repository_config()
        other=SimpleNamespace(login='Reviewer_work',identity={'login':'Reviewer_work','id':456})
        self.assertIsNone(g.record(self.config,other,self.pr(requested_reviewers=[])))
        self.assertEqual(g.inbox(),[])

    def test_repository_watch_still_holds_drafts_and_excludes_both_owned_authors(self):
        self.repository_config()
        self.assertEqual(self.record(requested_reviewers=[],draft=True)['status'],'draft_hold')
        self.assertEqual(self.record(requested_reviewers=[],draft=None)['status'],'draft_hold')
        for login in self.config['logins']:
            self.assertEqual(self.record(requested_reviewers=[],user={'login':login})['status'],'self_authored')

    def test_direct_and_repository_sources_deduplicate_into_one_request(self):
        self.record()
        self.repository_config()
        row=self.record()
        self.assertEqual(len(g.inbox()),1)
        self.assertTrue(row['assignment']['direct_request'])
        self.assertEqual(row['assignment']['repository_watch'],'Azure/repo')

    def test_repository_scope_preserves_existing_and_paused_ownership(self):
        self.repository_config()
        w.save(w.job_path('paused-owner'),{'id':'paused-owner','url':self.pr()['html_url'],'status':'paused'})
        row=self.record(requested_reviewers=[])
        self.assertEqual(row['status'],'linked');self.assertEqual(row['job'],'paused-owner')
        self.assertEqual(w.load(w.job_path('paused-owner'))['status'],'paused')

    def test_repository_watch_verification_does_not_require_a_direct_request(self):
        self.repository_config();row=self.record(requested_reviewers=[]);client=self.collector()
        with patch.object(g,'GitHubClient',return_value=client):
            self.assertTrue(g.verify_request(self.config,row))
            for pr in (self.pr(requested_reviewers=[],draft=True),self.pr(requested_reviewers=[],state='closed'),
                       self.pr(requested_reviewers=[],user={'login':'Reviewer_work'})):
                client.get.return_value=pr;self.assertFalse(g.verify_request(self.config,row))
            client.get.return_value=self.pr(requested_reviewers=[])
            self.assertFalse(g.verify_request({**self.config,'repository_watches':[]},row))
            self.assertFalse(g.verify_request({**self.config,'enabled':False},row))
            client.identity={'id':999,'login':'Reviewer'}
            self.assertFalse(g.verify_request(self.config,row))

    def test_repository_watch_verification_rejects_mismatched_routing_before_network(self):
        self.repository_config();row=self.record(requested_reviewers=[])
        row['assignment']['api_path']='repos/Azure/other/pulls/42'
        with patch.object(g,'GitHubClient') as client:
            self.assertFalse(g.verify_request(self.config,row));client.assert_not_called()

    def test_repository_list_paginates_without_the_search_thousand_result_limit(self):
        client=g.GitHubClient.__new__(g.GitHubClient);client.login='Reviewer';calls=[]
        def get(path):
            import urllib.parse
            calls.append(path);page=int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)['page'][0])
            return [{'id':n} for n in range((page-1)*100,page*100)] if page<=11 else []
        client.get=get
        self.assertEqual(len(client.repository_prs('Azure/repo')),1100)
        self.assertEqual(len(calls),12)
        self.assertIn('state=open',calls[0])

    def test_repository_pagination_rejects_repeated_pages_or_missing_collections(self):
        client=g.GitHubClient.__new__(g.GitHubClient);client.login='Reviewer'
        client.get=lambda path:[{'id':i} for i in range(100)]
        with self.assertRaisesRegex(RuntimeError,'did not advance'):client.repository_prs('Azure/repo')
        client.get=lambda path:{'message':'Not a collection'}
        with self.assertRaisesRegex(RuntimeError,'collection'):client.repository_prs('Azure/repo')

    def test_repository_watch_configuration_rejects_paths_unknown_accounts_and_ambiguous_owners(self):
        invalid=[{'repository':'https://github.com/Azure/repo','login':'Reviewer'},
                 {'repository':'Azure/repo?other=1','login':'Reviewer'},
                 {'repository':'Azure/../other','login':'Reviewer'},
                 {'repository':'Azure/repo','login':'unconfigured'}]
        for entry in invalid:
            with self.subTest(entry=entry),self.assertRaises(ValueError):
                g.repository_watches({**self.config,'repository_watches':[entry]})
        with self.assertRaises(ValueError):
            g.repository_watches({**self.config,'repository_watches':[
                {'repository':'Azure/repo','login':'Reviewer'},
                {'repository':'azure/REPO','login':'Reviewer_work'}]})
        self.assertEqual(g.repository_watches({**self.config,'repository_watches':[{'enabled':False}]}),[])

    def test_invalid_repository_configuration_is_durable_and_network_free(self):
        self.repository_config([{'repository':'../outside','login':'Reviewer'}])
        with patch.object(g,'GitHubClient') as client:g.poll();client.assert_not_called()
        state=w.load(g.state_path());self.assertFalse(state['complete']);self.assertIn('configuration_error',state)

    def test_repository_poll_collects_unassigned_prs_and_reports_coverage(self):
        self.repository_config();client=self.collector()
        with patch.object(g,'GitHubClient',return_value=client):g.poll()
        self.assertEqual(len(g.inbox()),1)
        row=g.inbox()[0];self.assertEqual(row['status'],'pending')
        coverage=g.coverage_summary()['accounts']['Reviewer']
        self.assertTrue(coverage['complete'])
        self.assertEqual(coverage['repositories']['Azure/repo']['without_direct_request'],1)

    def test_repository_poll_continues_when_direct_search_fails(self):
        self.repository_config();client=self.collector();client.requested.side_effect=RuntimeError('incomplete')
        with patch.object(g,'GitHubClient',return_value=client):g.poll()
        self.assertEqual(len(g.inbox()),1)
        coverage=g.coverage_summary()['accounts']['Reviewer'];self.assertFalse(coverage['complete'])
        self.assertTrue(coverage['repositories']['Azure/repo']['complete'])

    def test_one_repository_failure_does_not_suppress_another_repository(self):
        self.repository_config([{'repository':'Azure/repo','login':'Reviewer'},
                                {'repository':'Example/private-project','login':'Reviewer'}])
        client=self.collector()
        def prs(repository):
            if repository=='Example/private-project':raise RuntimeError('read unavailable')
            return [self.pr(requested_reviewers=[])]
        client.repository_prs.side_effect=prs
        with patch.object(g,'GitHubClient',return_value=client):g.poll()
        self.assertEqual(len(g.inbox()),1)
        coverage=g.coverage_summary()['accounts']['Reviewer']['repositories']
        self.assertTrue(coverage['Azure/repo']['complete'])
        self.assertFalse(coverage['Example/private-project']['complete'])

    def test_failed_repository_scan_does_not_withdraw_pending_work(self):
        self.repository_config();row=self.record(requested_reviewers=[]);client=self.collector()
        client.repository_prs.side_effect=RuntimeError('read unavailable')
        with patch.object(g,'GitHubClient',return_value=client):g.poll()
        self.assertEqual(g.inbox()[0]['status'],'pending')

    def test_complete_repository_scan_withdraws_only_missing_pending_work(self):
        self.repository_config();self.record(requested_reviewers=[]);client=self.collector(prs=[])
        with patch.object(g,'GitHubClient',return_value=client):g.poll()
        self.assertEqual(g.inbox()[0]['status'],'withdrawn')

    def test_repository_watch_remains_after_github_consumes_the_direct_request(self):
        self.repository_config();row=self.record()
        w.save(w.job_path('existing'),{'id':'existing','url':row['url'],'status':'waiting'})
        with patch.object(g,'GitHubClient',return_value=self.collector()):g.poll()
        self.assertEqual(g.inbox()[0]['status'],'linked')
        self.assertEqual(w.load(w.job_path('existing'))['status'],'waiting')

    def test_repository_poll_rejects_cross_repository_results(self):
        self.repository_config();client=self.collector(prs=[self.pr(html_url='https://github.com/Azure/other/pull/42')])
        with patch.object(g,'GitHubClient',return_value=client):g.poll()
        self.assertEqual(g.inbox(),[])
        self.assertFalse(g.coverage_summary()['accounts']['Reviewer']['repositories']['Azure/repo']['complete'])

    def test_repository_review_preparation_reuses_the_existing_dispatcher(self):
        self.repository_config();row=self.record(requested_reviewers=[])
        with patch.object(g,'GitHubClient',return_value=self.collector()),patch.object(teams_intake,'prepare_request',return_value=True) as worker:
            self.assertTrue(g.prepare_one(row['id']));worker.assert_called_once()
            self.assertEqual(worker.call_args[0][0]['assignment']['repository_watch'],'Azure/repo')

    def test_enrolled_repository_authorization_is_revocable_without_stopping_paused_intake_watches(self):
        self.repository_config()
        job={'id':'existing','url':self.pr()['html_url'],'github_login':'Reviewer','github_repository_watch':'Azure/repo'}
        self.assertTrue(g.enrolled_watch_authorized(job))
        w.save(g.config_path(),{**self.config,'enabled':False})
        self.assertTrue(g.enrolled_watch_authorized(job))
        w.save(g.config_path(),{**self.config,'repository_watches':[]})
        self.assertFalse(g.enrolled_watch_authorized(job))

    def test_repository_worker_prompt_distinguishes_explicit_scope_from_a_review_assignment(self):
        job={'id':'existing','mode':'review','github_login':'Reviewer','github_repository_watch':'Azure/repo'}
        prompt=w.worker_prompt(job,'/tmp/snapshot.json','test')
        self.assertIn('It does not require an individual review request',prompt)
        self.assertIn('rule remains enabled before comments or votes',prompt)
        self.assertIn('Never treat group membership or newly acquired administrator access as extra review authority',prompt)


from controller.fixtures import configured
GitHubIntakeTests.setUp = configured(GitHubIntakeTests.setUp)

if __name__=='__main__':unittest.main()
