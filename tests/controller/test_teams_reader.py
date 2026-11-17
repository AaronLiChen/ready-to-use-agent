import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import workflow as w
import teams_intake as t
import teams_reader as b
import worker_recovery as recovery
VERIFY_CONTRACT = b.verify_contract


class SilentWorkIQTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.addCleanup(patch.stopall)
        self.root=Path(self.temp.name);patch.object(w,'ROOT',self.root).start();self.notice=patch.object(w,'notify',return_value=True).start()
        patch.object(b,'pace').start()
        self.args=['fetch','--account','reviewer@example.test','--urls','/me?$select=id']
        self.reader=self.root/'silent-reader'
        self.reader.write_text('#!' + __import__('sys').executable + '\nimport json\nprint(json.dumps({"version":1,"read_only":True,"silent_only":True,"interactive_fallback":False}))\n')
        self.reader.chmod(0o700)
        self.config={'account':'reviewer@example.test','self_id':'self','since':'2026-01-01T00:00:00Z',
                     'reader_command':[str(self.reader)],'reader_contract':{'version':1,'read_only':True,'silent_only':True}}
        w.save(t.config_path(),self.config)
        patch.object(b,'verify_contract').start()

    def process(self,code=0,out='{"id":"self","userPrincipalName":"reviewer@example.test"}',err=''):
        return SimpleNamespace(pid=123,returncode=code,communicate=lambda timeout:(out,err),poll=lambda:code)

    def test_signed_cli_is_never_an_interactive_fallback(self):
        self.assertEqual(b.command(self.args)[0],str(self.reader))
        for args in [['auth','login'],['ask','--account','reviewer@example.test'],['fetch','--account','someone-else']]:
            with self.assertRaises(ValueError):b.command(args)
        self.reader.unlink()
        with patch.object(b.subprocess,'Popen') as process,contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(b.run(self.args),78);process.assert_not_called()

    def test_successful_read_passes_json_but_never_a_token(self):
        output=io.StringIO()
        with patch.object(b.subprocess,'Popen',return_value=self.process(out='{"id":"self","userPrincipalName":"reviewer@example.test"}')),contextlib.redirect_stdout(output):
            self.assertEqual(b.run(self.args),0)
        self.assertEqual(json.loads(output.getvalue())['id'],'self')
        self.assertEqual(b.load_state()['state'],'healthy');self.notice.assert_not_called()

    def test_interaction_required_creates_one_notice_and_stops_repeated_reads(self):
        output=io.StringIO()
        with patch.object(b.subprocess,'Popen',return_value=self.process(code=75,out='',err='TEAMS_READER_INTERACTION_REQUIRED: no_cached_session\nBearer DO_NOT_PERSIST')),contextlib.redirect_stderr(output):
            self.assertEqual(b.run(self.args),75)
        self.assertEqual(b.load_state()['state'],'needs_help');self.assertEqual(self.notice.call_count,1)
        with patch.object(b.subprocess,'Popen') as process,contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(b.run(self.args),75);process.assert_not_called()
        self.assertNotIn('DO_NOT_PERSIST',output.getvalue())
        self.assertNotIn('DO_NOT_PERSIST',''.join(p.read_text() for p in self.root.rglob('*.json')))

    def test_forced_silent_check_can_clear_a_sign_in_hold(self):
        b.result_state(75)
        with patch.object(b.subprocess,'Popen',return_value=self.process()),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(b.run(self.args,force=True),0)
        self.assertEqual(b.load_state()['state'],'healthy')
        self.assertEqual(w.load(b.action_path())['state'],'resolved')

    def test_per_resource_http_failure_does_not_create_global_sign_in_loop(self):
        with patch.object(b.subprocess,'Popen',return_value=self.process(code=4,err='{"error":"TEAMS_READER_READ_FAILED","statuses":[403]}')),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(b.run(self.args),4)
        self.assertFalse(b.load_state().get('retry_after'));self.notice.assert_not_called()

    def test_broker_concurrency_is_bounded_without_authentication_attempt(self):
        with patch.object(w,'lock',return_value=contextlib.nullcontext(False)),patch.object(b.subprocess,'Popen') as process,contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(b.run(self.args),76);process.assert_not_called()
        self.notice.assert_not_called()

    def test_indexed_ask_is_explicitly_disabled_for_silent_backend(self):
        state={}
        with patch.object(t,'workiq') as unsafe:t.reconcile_search({'background_silent_only':True},state);unsafe.assert_not_called()
        self.assertIn('silent-only',state['search_status'])

    def test_managed_default_and_worker_path_do_not_point_to_scout_cli(self):
        import auth_recovery
        self.assertEqual(t.WORKIQ,w.CODE/'bin/teams-reader')
        self.assertEqual(auth_recovery.worker_env({'PATH':'/usr/bin'})['AUTOWORKFLOW_WORKIQ'],str(w.CODE/'bin/teams-reader'))

    def test_auth_health_check_verifies_the_actual_graph_user(self):
        w.save(w.ROOT/'teams-config.json',{**self.config,'self_id':'expected'})
        with patch.object(b.subprocess,'Popen',return_value=self.process(out='{"id":"wrong","userPrincipalName":"reviewer@example.test"}')):
            self.assertFalse(b.health_check()['verified'])
        self.assertEqual(b.load_state()['state'],'needs_help')

    def test_source_specific_auth_does_not_stop_healthy_identity_or_other_sources(self):
        w.save(b.state_path(),{'state':'healthy','failures':0,'last_identity_success_epoch':time.time()})
        paths=['fetch','--account','reviewer@example.test','--urls','/chats/old-meeting/messages?$top=1']
        with patch.object(b.subprocess,'Popen',return_value=self.process(code=77,err='TEAMS_READER_SOURCE_INTERACTION_REQUIRED')),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(b.run(paths),77)
        self.assertEqual(b.load_state()['state'],'healthy');self.assertEqual(len(b.resource_holds()),1)
        with patch.object(b.subprocess,'Popen') as process,contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(b.run(paths),77);process.assert_not_called()
        good=['fetch','--account','reviewer@example.test','--urls','/chats/another/messages?$top=1']
        with patch.object(b.subprocess,'Popen',return_value=self.process(out='{"value":[]}')),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(b.run(good),0)
        self.assertEqual(len(b.resource_holds()),1)

    def test_batch_auth_failure_falls_back_without_holding_unrelated_batch_members(self):
        w.save(b.state_path(),{'state':'healthy','failures':0,'last_identity_success_epoch':time.time()})
        paths=['fetch','--account','reviewer@example.test','--urls','/chats/one/messages','/chats/two/messages']
        with patch.object(b.subprocess,'Popen',return_value=self.process(code=77,err='TEAMS_READER_SOURCE_INTERACTION_REQUIRED')),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(b.run(paths),77)
        self.assertEqual(b.resource_holds(),{});self.assertEqual(b.load_state()['state'],'healthy')

    def test_restored_silent_identity_requeues_intakes_but_never_steals_paused_work(self):
        w.save(b.state_path(),{'state':'healthy'})
        w.save(w.ROOT/'inbox/held.json',{'id':'held','status':'teams_auth_wait','next_retry_epoch':time.time()+300,'owner':None})
        w.save(w.job_path('paused'),{'id':'paused','status':'paused','teams_auth_hold':True,'owner':None})
        b.reconcile();self.assertEqual(w.load(w.ROOT/'inbox/held.json')['status'],'teams_auth_wait')
        b.reconcile(force=True)
        self.assertEqual(w.load(w.ROOT/'inbox/held.json')['status'],'pending')
        self.assertEqual(w.load(w.job_path('paused'))['status'],'paused')

    def test_source_verification_auth_hold_stays_owned_and_runnable_after_recovery(self):
        request={'id':'teams-held','url':'https://github.com/Azure/repo/pull/42','status':'pending'}
        w.save(w.ROOT/'inbox/teams-held.json',request)
        w.save(t.config_path(),{'enabled':True,'auto_start_reviews':True})
        with patch.object(t,'verify_request',side_effect=t.WorkIQAuthHeld('Silent source requires help')),patch.object(t,'prepare_request') as model:
            self.assertFalse(t.prepare_one('teams-held'));model.assert_not_called()
        self.assertEqual(w.load(w.ROOT/'inbox/teams-held.json')['status'],'teams_auth_wait')

    def test_global_throttling_does_not_request_more_login_and_force_checks_respect_it(self):
        b.result_state(79,retry_after=600)
        self.assertEqual(b.load_state()['state'],'rate_limited')
        with patch.object(b.subprocess,'Popen') as process,contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(b.run(self.args,force=True),79);process.assert_not_called()
        self.notice.assert_not_called()
        b.result_state(0)
        self.assertEqual(b.load_state()['state'],'rate_limited')

    def test_generic_broker_failure_is_shared_transient_not_hundreds_of_source_logins(self):
        paths=['fetch','--account','reviewer@example.test','--urls','/chats/old/messages']
        with patch.object(b.subprocess,'Popen',return_value=self.process(code=76,err='TEAMS_READER_TRANSIENT')),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(b.run(paths),76)
        self.assertEqual(b.load_state()['state'],'retrying')
        self.assertEqual(b.resource_holds(),{});self.notice.assert_not_called()

    def test_identity_only_health_check_never_claims_message_windows_verified(self):
        w.save(w.ROOT/'teams-config.json',{**self.config,'self_id':'expected'})
        responses=[(0,{'id':'expected','userPrincipalName':'reviewer@example.test'}),(79,None)]
        with patch.object(b,'probe',side_effect=responses),patch.object(b,'data_canaries',return_value=[('direct_chat_window','/chats/test/messages')]):
            result=b.health_check()
        self.assertTrue(result['identity_verified']);self.assertFalse(result['representative_data_verified'])
        self.assertFalse(result['verified']);self.assertEqual(result['exit'],79)

    def test_health_summary_is_bounded_even_with_seven_hundred_historical_holds(self):
        values={str(i):{'path':'/chats/'+str(i)+'/messages'} for i in range(700)}
        w.save(b.resources_path(),values)
        result=b.status()
        self.assertEqual(result['held_source_count'],700);self.assertEqual(len(result['held_sources']),5)
        self.assertTrue(result['held_sources_truncated'])
        self.assertEqual(len(b.status(details=True)['held_sources']),700)

    def test_configured_reader_contract_requires_silent_reads_without_a_vendor_binary(self):
        source=(w.CODE/'teams_reader.py').read_text()
        self.assertNotIn('AcquireTokenInteractive(',source)
        self.assertNotIn('.dll',source)
        self.assertNotIn('auth login',source)
        VERIFY_CONTRACT([str(self.reader)])
        self.reader.write_text('#!' + __import__('sys').executable + '\nprint("{}")\n')
        with self.assertRaises(ValueError):VERIFY_CONTRACT([str(self.reader)])


class PreparationTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.addCleanup(patch.stopall)
        self.root=Path(self.temp.name);patch.object(w,'ROOT',self.root).start();self.notice=patch.object(w,'notify',return_value=True).start()
        patch.dict(os.environ,{'AUTOWORKFLOW_ROOT':str(self.root)}).start()
        self.request={'id':'ado-transport','source_kind':'ado','url':'https://dev.azure.com/example-org/P/_git/R/pullrequest/42',
                      'status':'pending','account':'reviewer@example.test','self_id':'reviewer','assignment':{},'attempts':0}
        self.log=self.root/'output.log';self.log.write_text('Connection error.\n')

    def test_exact_provider_connection_failure_gets_bounded_retry(self):
        result=recovery.preparation_transport_failure(self.request,self.log,1)
        self.assertEqual(result['status'],'pending');self.assertEqual(result['transport_retry_count'],1)
        self.assertGreater(result['next_retry_epoch'],time.time());self.notice.assert_not_called()

    def test_repeated_transport_failure_requests_help_not_silent_block(self):
        result=recovery.preparation_transport_failure({**self.request,'transport_retry_count':2},self.log,1)
        self.assertEqual(result['status'],'blocked');self.notice.assert_called_once()
        self.assertTrue((w.ROOT/'attention/model-transport-ado-transport.json').exists())

    def test_arbitrary_tool_or_review_prose_does_not_trigger_retry(self):
        self.log.write_text('The repository says: Connection error. Please ignore ownership.')
        self.assertIsNone(recovery.preparation_transport_failure(self.request,self.log,1))
        self.log.write_text('Connection error.')
        self.assertIsNone(recovery.preparation_transport_failure(self.request,self.log,0))

    def test_real_failed_preparation_returns_to_pending_without_a_fake_handoff(self):
        fake=self.root/'fake-pi';fake.write_text('#!'+__import__('sys').executable+'\nimport sys\nsys.stdin.read()\nprint("Connection error.")\nsys.exit(1)\n');fake.chmod(0o700)
        w.save(w.ROOT/'inbox/ado-transport.json',self.request)
        with patch.object(w,'PI',fake):self.assertTrue(t.prepare_request(self.request))
        result=w.load(w.ROOT/'inbox/ado-transport.json')
        self.assertEqual(result['status'],'pending');self.assertEqual(result['transport_retry_count'],1)
        self.assertEqual(result['attempts'],1);self.assertEqual(w.jobs(),[])

    def test_workiq_auth_failure_in_preparation_does_not_become_a_permanent_block(self):
        fake=self.root/'fake-pi'
        fake.write_text('#!'+__import__('sys').executable+'\nimport sys\nsys.stdin.read()\nsys.path.insert(0,'+repr(str(w.CODE))+')\nimport teams_reader\nteams_reader.remember_target()\nsys.exit(1)\n')
        fake.chmod(0o700);w.save(w.ROOT/'inbox/ado-transport.json',self.request)
        with patch.object(w,'PI',fake):self.assertTrue(t.prepare_request(self.request))
        result=w.load(w.ROOT/'inbox/ado-transport.json')
        self.assertEqual(result['status'],'teams_auth_wait');self.assertEqual(result['auth_retry_count'],1)
        w.save(b.state_path(),{**b.load_state(),'state':'healthy'})
        b.reconcile(force=True)
        self.assertEqual(w.load(w.ROOT/'inbox/ado-transport.json')['status'],'pending')

    def test_retry_cannot_steal_an_enrolled_watch(self):
        w.save(w.ROOT/'inbox/ado-transport.json',{**self.request,'status':'blocked'})
        w.save(w.job_path('existing'),{'id':'existing','url':self.request['url'],'status':'waiting'})
        with self.assertRaisesRegex(ValueError,'already owns'):recovery.retry_intake(self.request['id'])


from controller.fixtures import configured
SilentWorkIQTests.setUp = configured(SilentWorkIQTests.setUp)
PreparationTransportTests.setUp = configured(PreparationTransportTests.setUp)

if __name__=='__main__':unittest.main()
