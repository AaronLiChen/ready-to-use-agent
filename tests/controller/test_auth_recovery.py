import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

import workflow as w
import auth_recovery as a
import parallel_dispatch as dispatch
import teams_intake
import ado_intake


class AuthRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.addCleanup(patch.stopall)
        self.root=Path(self.temp.name);patch.object(w,'ROOT',self.root).start()
        patch.dict(os.environ,{'AUTOWORKFLOW_ROOT':str(self.root)}).start()
        for key in ['AUTOWORKFLOW_JOB','AUTOWORKFLOW_INTAKE','AUTOWORKFLOW_AUTH_KIND','AUTOWORKFLOW_AUTH_ID']:os.environ.pop(key,None)
        self.notice=patch.object(w,'notify',return_value=True).start()
        self.context={'kind':'job','id':'review-one','url':'https://dev.azure.com/example-org/Project/_git/repo/pullrequest/42'}

    def job(self,**changes):
        value={'id':'review-one','url':self.context['url'],'mode':'review','status':'waiting','owner':None,
            'resource_key':'owned','cycles':[],'max_cycles_per_day':12,'fingerprint':'unchanged'}
        value.update(changes);w.save(w.job_path(value['id']),value);return value

    def test_first_successful_token_always_verifies_the_operator(self):
        with patch.object(a,'_raw_token',return_value='FIXTURE_TOKEN'),patch.object(a,'verify_token') as verify:
            a.acquire(a.load_skill(),self.context)
        verify.assert_called_once_with('FIXTURE_TOKEN')
        self.assertIsNotNone(a.load_state().get('verified_epoch'))

    def test_old_identity_verification_is_not_reused_indefinitely(self):
        w.save(a.state_path(),{'state':'healthy','expected_identity_id':'fixture-user','verified_epoch':time.time()-301})
        with patch.object(a,'_raw_token',return_value='FIXTURE_TOKEN'),patch.object(a,'verify_token') as verify:
            a.acquire(a.load_skill(),self.context)
        verify.assert_called_once_with('FIXTURE_TOKEN')

    def test_three_failed_refreshes_escalate_once_not_once_per_pr(self):
        for _ in range(3):a.failure('timeout',self.context)
        state=a.load_state();self.assertEqual(state['state'],'needs_sign_in');self.assertEqual(state['attempts'],3)
        self.assertEqual(self.notice.call_count,1)
        a.failure('deferred',{'kind':'job','id':'review-two','url':self.context['url'].replace('/42','/43')})
        a.tick();self.assertEqual(self.notice.call_count,1)
        self.assertEqual(len(a.status()['affected']),2)
        self.assertIn('az login',a.status()['human_action']['commands'])

    def test_failed_probe_gets_an_auth_hold_instead_of_a_generic_stall(self):
        self.job()
        def fail(job):
            a.failure('sign_in',self.context)
            raise a.AuthError('sign_in')
        with patch.object(w,'snapshot',side_effect=fail):w.tick_one('review-one')
        job=w.load(w.job_path('review-one'))
        self.assertEqual(job['status'],'waiting');self.assertTrue(job['auth_hold'])
        self.assertIn('auth-status',job['probe_error'])
        a.recovered();a.reconcile()
        self.assertEqual(w.load(w.job_path('review-one'))['status'],'waiting')
        self.assertIsNone(w.load(w.job_path('review-one')).get('wake_at'))
        self.assertIsNotNone(w.load(w.job_path('review-one')).get('probe_retry_at'))

    def test_raw_cli_failure_does_not_expose_secret_error_output(self):
        skill=a.load_skill();secret='SECRET_TOKEN_VALUE_MUST_NOT_ESCAPE'
        failure=subprocess.CalledProcessError(1,'az',output=secret,stderr='AADSTS50076 '+secret)
        with patch.object(a,'real_azure_cli',return_value='/fixture/az'),patch.object(subprocess,'run',side_effect=failure):
            with self.assertRaises(a.AuthError) as raised:a.acquire(skill,self.context)
        self.assertNotIn(secret,str(raised.exception))
        self.assertNotIn(secret,''.join(p.read_text() for p in self.root.rglob('*.json')))
        self.assertNotIn(secret,str(self.notice.call_args_list))
        self.assertEqual(a.load_state()['state'],'needs_sign_in')

    def test_probe_cache_contention_does_not_restart_an_unchanged_review(self):
        import probes
        data={'state':'active','head':'a'*40,'target':'refs/heads/main','source':'refs/heads/source'}
        self.job(fingerprint=probes.fingerprint(data))
        with patch.object(w,'snapshot',side_effect=a.AuthError('busy')):w.tick_one('review-one')
        job=w.load(w.job_path('review-one'))
        self.assertEqual(job['status'],'waiting');self.assertTrue(job['auth_probe_deferred'])
        self.assertNotIn('probe_error',job);self.assertNotIn('wake_at',job)
        with patch.object(w,'snapshot',return_value=data),patch.object(w,'run_worker') as worker:
            w.tick_one('review-one');worker.assert_not_called()
        self.assertNotIn('auth_probe_deferred',w.load(w.job_path('review-one')))

    def test_busy_probe_is_not_a_pending_model_action(self):
        self.job();a.failure('busy',self.context)
        self.assertFalse(a.held('job','review-one'))
        self.assertEqual(a.reconcile(),[])
        with patch.dict(os.environ,{'AUTOWORKFLOW_JOB':'review-one'}):a.failure('busy',self.context)
        self.assertTrue(a.held('job','review-one'))

    def test_forced_check_reports_when_verification_was_deferred(self):
        with patch.object(a,'acquire',side_effect=a.AuthError('busy')):result=a.tick(force=True)
        self.assertFalse(result['verification_performed']);self.assertEqual(result['check_error'],'busy')

    def test_auth_interruption_does_not_exhaust_pr_allowance_or_erase_global_usage(self):
        now=time.time();job=self.job(cycles=[now-1],max_cycles_per_day=1,auth_cycle_times=[now-1])
        self.assertTrue(w.reserve_cycle(job))
        self.assertEqual(len(job['cycles']),2);self.assertEqual(len(w.recent_cycles()),2)
        self.assertFalse(w.reserve_cycle(job))

    def test_expired_login_or_mfa_escalates_immediately(self):
        a.failure('sign_in',self.context)
        self.assertEqual(a.load_state()['state'],'needs_sign_in')
        self.assertEqual(self.notice.call_count,1)

    def test_probe_only_recovery_preserves_event_driven_wait_without_model_work(self):
        import probes
        data={'state':'active','head':'a'*40,'target':'refs/heads/main','source':'refs/heads/source'}
        self.job(fingerprint=probes.fingerprint(data))
        with patch.object(w,'snapshot',side_effect=a.AuthError('timeout')):
            a.failure('timeout',self.context);w.tick_one('review-one')
        a.recovered();a.reconcile()
        with patch.object(w,'snapshot',return_value=data),patch.object(w,'run_worker') as model:
            w.tick_one('review-one');model.assert_not_called()

    def test_model_interruption_is_not_downgraded_by_later_probe_deferrals(self):
        self.job()
        with patch.dict(os.environ,{'AUTOWORKFLOW_JOB':'review-one'}):a.failure('timeout',self.context)
        a.failure('deferred',self.context)
        self.assertEqual(a.load_state()['affected']['job:review-one']['phase'],'model')
        a.recovered();a.reconcile()
        self.assertEqual(w.load(w.job_path('review-one'))['status'],'ready')

    def test_backoff_prevents_a_refresh_storm(self):
        a.failure('timeout',self.context)
        with patch.object(a,'_raw_token') as refresh:
            for _ in range(5):
                with self.assertRaises(a.AuthError):a.acquire(SimpleNamespace(),self.context)
            refresh.assert_not_called()
        self.assertEqual(a.load_state()['attempts'],1)

    def test_recovery_retries_even_with_unchanged_pr_fingerprint(self):
        self.job(pending_action='comment');a.failure('timeout',self.context)
        with patch.object(a,'_raw_token',return_value='FIXTURE_TOKEN'),patch.object(a,'verify_token',return_value={'account':'reviewer@example.test'}):
            result=a.tick(force=True)
        job=w.load(w.job_path('review-one'))
        self.assertEqual(job['status'],'ready');self.assertEqual(job['fingerprint'],'unchanged')
        self.assertFalse(a.held('job','review-one'))
        self.assertEqual(len(result['resumed']),1)

    def test_a_job_missing_its_checkpoint_can_wait_for_auth_not_be_abandoned(self):
        job=self.job(status='active');a.failure('sign_in',self.context)
        a.hold_job(job);w.save(w.job_path(job['id']),job)
        self.assertEqual(job['status'],'waiting');self.assertTrue(job['auth_hold'])
        self.assertFalse(a.may_run('job',job['id']))
        a.recovered();a.reconcile()
        self.assertEqual(w.load(w.job_path(job['id']))['status'],'ready')

    def test_paused_and_terminal_jobs_never_resume(self):
        for state in ['paused','done']:
            self.job(status=state);a.failure('timeout',self.context);a.recovered();a.reconcile()
            self.assertEqual(w.load(w.job_path('review-one'))['status'],state)

    def test_live_worker_and_foreground_owner_are_not_stolen(self):
        for values in [{'worker_pid':os.getpid(),'worker_identity':w.process_identity(os.getpid())},
                       {'owner':{'pid':os.getpid(),'identity':w.process_identity(os.getpid())}}]:
            self.job(**values);a.failure('timeout',self.context);a.recovered()
            self.assertEqual(a.reconcile(),[])
            self.assertTrue(a.held('job','review-one'))

    def test_separate_scope_blocker_is_not_waived(self):
        self.job(status='blocked',decision='Explicit report-only task: do not post')
        a.failure('timeout',self.context);a.recovered();a.reconcile()
        self.assertEqual(w.load(w.job_path('review-one'))['status'],'blocked')
        self.assertTrue(any(x['state']=='needs_job_decision' for x in a.status()['action_required']))

    def test_auth_only_intake_failure_can_resume_without_losing_attempt_history(self):
        context={'kind':'intake','id':'ado-review','url':self.context['url']}
        w.save(w.ROOT/'inbox/ado-review.json',{'id':'ado-review','url':context['url'],'status':'auth_wait','attempts':2,'auth_retry_count':2})
        a.failure('timeout',context);a.recovered();a.reconcile()
        request=w.load(w.ROOT/'inbox/ado-review.json')
        self.assertEqual(request['status'],'pending');self.assertEqual(request['attempts'],2)
        self.assertEqual(request['auth_retry_count'],2)

    def test_permission_denial_does_not_block_unrelated_prs_or_fake_reauthentication(self):
        self.job();a.permission_failure(self.context,'https://dev.azure.com/example-org/Project/_apis/example')
        self.assertEqual(a.load_state()['state'],'healthy')
        self.assertFalse(a.may_run('job','review-one'));self.assertTrue(a.may_run('job','unrelated'))
        a.reconcile();self.assertEqual(w.load(w.job_path('review-one'))['status'],'waiting')
        self.assertTrue(any(x['state']=='needs_access' for x in a.status()['action_required']))

    def test_identity_change_never_resumes_work(self):
        self.job();a.failure('timeout',self.context)
        with patch.object(a,'_raw_token',return_value='FIXTURE_TOKEN'),patch.object(a,'verify_token',side_effect=a.AuthError('identity_mismatch')):
            result=a.tick(force=True)
        self.assertEqual(result['state']['state'],'identity_mismatch')
        self.assertEqual(w.load(w.job_path('review-one'))['status'],'waiting')

    def test_no_tokens_or_cli_output_are_written_to_state_or_notifications(self):
        a.failure('sign_in',self.context)
        text=''.join(p.read_text() for p in self.root.rglob('*.json'))
        self.assertNotIn('Authorization',text);self.assertNotIn('accessToken',text)
        self.assertNotIn('Bearer',str(self.notice.call_args_list))

    def test_healthy_periodic_tick_is_network_and_model_free(self):
        with patch.object(a,'acquire') as token,patch.object(w,'run_worker') as model:
            a.tick();token.assert_not_called();model.assert_not_called()

    def test_health_probe_detects_login_recovery_without_user_command(self):
        self.job(pending_action='comment');a.failure('sign_in',self.context)
        state=a.load_state();state['next_probe_epoch']=time.time()-1;w.save(a.state_path(),state)
        with patch.object(a,'_raw_token',return_value='FIXTURE_TOKEN'),patch.object(a,'verify_token',return_value={'account':'reviewer@example.test'}):
            a.tick()
        self.assertEqual(a.load_state()['state'],'healthy')
        self.assertEqual(w.load(w.job_path('review-one'))['status'],'ready')

    def test_busy_cache_lock_is_not_a_login_failure(self):
        original=w.lock
        def lock(name,**kwargs):return contextlib.nullcontext(False) if name=='ado-auth' else original(name,**kwargs)
        with patch.object(w,'lock',side_effect=lock),patch.object(a.time,'monotonic',side_effect=[0,7]),patch.object(a,'_raw_token') as token:
            with self.assertRaises(a.AuthError):a.acquire(SimpleNamespace(),self.context)
            token.assert_not_called()
        self.assertEqual(a.load_state()['state'],'healthy');self.notice.assert_not_called()

    def test_raw_skill_auth_keeps_its_api_but_bounds_azure_cli_timeout(self):
        skill=a.load_skill()
        with patch.object(a,'real_azure_cli',return_value='/fixture/az'),patch.object(subprocess,'run',return_value=SimpleNamespace(stdout='header.payload.signature\n',returncode=0)) as run:
            self.assertEqual(a._raw_token(skill),'header.payload.signature')
        self.assertEqual(run.call_args.args[0][0],'/fixture/az')
        self.assertEqual(run.call_args.kwargs['timeout'],12)

    def test_http_401_is_auth_recovery_not_an_empty_pr_result(self):
        error=urllib.error.HTTPError('https://dev.azure.com/example-org/',401,'Unauthorized',{},None)
        with self.assertRaises(a.AuthError):a.http_failure(error,self.context)
        self.assertEqual(a.load_state()['state'],'retrying')

    def test_http_403_requires_access_help_not_blanket_iam_changes(self):
        error=urllib.error.HTTPError('https://dev.azure.com/example-org/',403,'Forbidden',{},None)
        with self.assertRaises(a.AuthError):a.http_failure(error,self.context,'https://dev.azure.com/example-org/')
        self.assertTrue(a.held('job','review-one')['permission_pending'])

    def test_older_waiting_writer_without_retry_flag_is_discovered_once(self):
        self.job(next_action='After ADO authentication recovers, post the prepared finding',last_checkpoint_cycle='old-cycle',wake_at=None)
        with patch.object(a,'_raw_token',return_value='FIXTURE_TOKEN'),patch.object(a,'verify_token',return_value={'account':'reviewer@example.test'}):
            a.tick()
        self.assertEqual(w.load(w.job_path('review-one'))['status'],'ready')
        state=a.load_state();self.assertEqual(state['legacy_scanned']['job:review-one'],'old-cycle')
        a.tick();self.assertFalse(a.held('job','review-one'))

    def test_older_auth_failed_preparation_is_requeued_with_history_preserved(self):
        log=self.root/'logs/prep.log';log.parent.mkdir();log.write_text('Azure CLI token acquisition timed out. No job was enrolled.')
        w.save(self.root/'inbox/ado-old.json',{'id':'ado-old','url':self.context['url'],'status':'blocked','last_log':str(log),'attempts':2})
        with patch.object(a,'_raw_token',return_value='FIXTURE_TOKEN'),patch.object(a,'verify_token',return_value={'account':'reviewer@example.test'}):a.tick()
        request=w.load(self.root/'inbox/ado-old.json')
        self.assertEqual(request['status'],'pending');self.assertEqual(request['attempts'],2)
        self.assertEqual(request['auth_retry_count'],1)

    def test_author_reply_wait_is_not_mistaken_for_authentication_failure(self):
        self.job(next_action='Observe author replies and post only after a real code fix',last_checkpoint_cycle='normal-cycle')
        with patch.object(a,'acquire') as token:a.tick();token.assert_not_called()
        self.assertEqual(w.load(w.job_path('review-one'))['status'],'waiting')

    def test_normal_pre_write_auth_verification_is_not_a_stalled_publication(self):
        self.job(next_action='Await author fixes; verify authentication before posting or approving',last_checkpoint_cycle='normal-cycle')
        with patch.object(a,'acquire') as token:a.tick();token.assert_not_called()
        self.assertEqual(w.load(w.job_path('review-one'))['status'],'waiting')

    def test_supervisor_context_is_inherited_by_legacy_skill_calls(self):
        self.job()
        with patch.dict(os.environ,{'AUTOWORKFLOW_AUTH_KIND':'job','AUTOWORKFLOW_AUTH_ID':'review-one'}):
            self.assertEqual(a.target()['id'],'review-one')
        env=a.worker_env({'PATH':'/usr/bin'})
        self.assertTrue(env['PATH'].startswith(str(w.CODE/'bin')+os.pathsep))

    def test_real_worker_auth_failure_without_checkpoint_remains_recoverable(self):
        job=self.job(status='ready',workspace=str(self.root),branch='review',cycle_timeout=5,
                     goal='Review fixture',scope='Fixture only',acceptance='Verified review')
        script=self.root/'fake-pi'
        script.write_text('#!'+sys.executable+'\nimport sys\nsys.stdin.read()\nsys.path.insert(0,'+repr(str(w.CODE))+')\nimport workflow,auth_recovery\nworkflow.notify=lambda text:True\nauth_recovery.failure("sign_in")\n')
        script.chmod(0o700)
        with patch.object(w,'PI',script):w.run_worker(job,self.root/'snapshot.json')
        latest=w.load(w.job_path(job['id']))
        self.assertEqual(latest['status'],'waiting');self.assertTrue(latest['auth_hold'])
        self.assertEqual(a.load_state()['state'],'needs_sign_in')
        a.recovered();a.reconcile()
        self.assertEqual(w.load(w.job_path(job['id']))['status'],'ready')

    def test_real_preparation_auth_failure_does_not_exhaust_normal_attempts(self):
        request={'id':'ado-prep','url':self.context['url'],'source_kind':'ado','status':'pending',
                 'account':'reviewer@example.test','self_id':'fixture-user','assignment':{},'attempts':2,'auth_retry_count':2}
        w.save(w.ROOT/'inbox/ado-prep.json',request)
        script=self.root/'fake-pi'
        script.write_text('#!'+sys.executable+'\nimport sys\nsys.stdin.read()\nsys.path.insert(0,'+repr(str(w.CODE))+')\nimport workflow,auth_recovery\nworkflow.notify=lambda text:True\nauth_recovery.failure("sign_in")\n')
        script.chmod(0o700)
        with patch.object(w,'PI',script):self.assertTrue(teams_intake.prepare_request(request))
        latest=w.load(w.ROOT/'inbox/ado-prep.json')
        self.assertEqual(latest['status'],'auth_wait');self.assertEqual(latest['attempts'],3)
        self.assertEqual(latest['auth_retry_count'],3)
        a.recovered();a.reconcile()
        self.assertEqual(w.load(w.ROOT/'inbox/ado-prep.json')['status'],'pending')

    def test_managed_cli_adapter_returns_only_the_token(self):
        script=w.CODE/'bin/az'
        code="import sys,runpy;sys.path.insert(0,"+repr(str(w.CODE))+");import auth_recovery as a;a.load_skill=lambda:None;a.acquire=lambda skill:'FIXTURE_ONLY';sys.argv=['az','account','get-access-token','--resource',a.RESOURCE,'--query','accessToken','-o','tsv'];runpy.run_path("+repr(str(script))+",run_name='__main__')"
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout,'FIXTURE_ONLY\n')

    def test_managed_cli_failure_returns_safe_marker_not_token_or_raw_error(self):
        script=w.CODE/'bin/az'
        code="import sys,runpy;sys.path.insert(0,"+repr(str(w.CODE))+");import auth_recovery as a;a.load_skill=lambda:None\ndef fail(skill):raise a.AuthError('sign_in')\na.acquire=fail;sys.argv=['az','account','get-access-token','--resource',a.RESOURCE,'--query','accessToken','-o','tsv'];runpy.run_path("+repr(str(script))+",run_name='__main__')"
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,75);self.assertEqual(result.stdout,'')
        self.assertIn('AUTOWORKFLOW_AUTH_WAIT',result.stderr);self.assertNotIn('Traceback',result.stderr)


from controller.fixtures import configured
AuthRecoveryTests.setUp = configured(AuthRecoveryTests.setUp)

if __name__=='__main__':unittest.main()
