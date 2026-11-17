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

import workflow as w
import parallel_dispatch as p


FAKE_RUNNER = r'''
import os,sys,time,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import workflow as w
import parallel_dispatch as p
kind,name,reservation=sys.argv[2:]
markers=w.ROOT/'markers';markers.mkdir(exist_ok=True)
def stamp(suffix):
 (markers/(name+suffix)).write_text(str(time.time()))
def wait_for(pattern,count):
 deadline=time.time()+4
 while len(list(markers.glob(pattern)))<count and time.time()<deadline:time.sleep(.02)
def data(job):return {'head':'a'*40,'state':'open','target':'refs/heads/main','source':'refs/heads/source','threads':[]}
w.snapshot=data
w.workspace_problem=lambda job:None
def worker(job,snapshot):
 if not w.reserve_cycle(job):return False
 stamp('.start')
 wait_for('*.ready',int(os.environ.get('FIXTURE_READY','1')))
 time.sleep(.35)
 current=w.load(w.job_path(job['id']));current.update(status='waiting',owner=None);w.save(w.job_path(job['id']),current)
 stamp('.end');return True
w.run_worker=worker
class Adapter:
 def prepare_one(self,request_id):
  stamp('.prep-start');wait_for('*.prep-start',int(os.environ.get('FIXTURE_PREPS','1')))
  time.sleep(.8 if name.endswith('slow') else .05)
  request=w.load(w.ROOT/'inbox'/(name+'.json'))
  job={'id':name,'mode':'review','url':request['url'],'status':'ready','intake':name,'branch':'review',
       'workspace':str(w.ROOT/name),'resource_key':name,'cycles':[],'max_cycles_per_day':12,'owner':None}
  w.save(w.job_path(name),job);stamp('.prep-end');return True
w.intake_adapter=lambda request:Adapter()
stamp('.ready')
p.execute(kind,name,reservation)
'''


class ParallelDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.addCleanup(patch.stopall)
        self.root=Path(self.temp.name);patch.object(w,'ROOT',self.root).start();patch.object(w,'notify').start()
        patch.dict(os.environ,{'AUTOWORKFLOW_ROOT':str(self.root)}).start()
        for key in ['AUTOWORKFLOW_JOB','AUTOWORKFLOW_INTAKE','AUTOWORKFLOW_CYCLE']:os.environ.pop(key,None)
        w.save(self.root/'scheduler-config.json',{'max_parallel_workers':64,'global_cycles_per_day':1024})

    def url(self,number):return 'https://dev.azure.com/example-org/Project/_git/repo/pullrequest/'+str(number)

    def job(self,name,number,**extra):
        job={'id':name,'url':self.url(number),'status':'ready','mode':'review','owner':None,'branch':'review',
             'workspace':str(self.root/name),'resource_key':name,'cycles':[],'max_cycles_per_day':12}
        job.update(extra);w.save(w.job_path(name),job);return job

    def request(self,name,number,kind='ado',**extra):
        adapter=w.intake_adapter({'source_kind':kind})
        w.save(adapter.config_path(),{'enabled':True,'auto_start_reviews':True})
        value={'id':name,'url':self.url(number),'source_kind':kind,'status':'pending','created_at':w.utc()}
        value.update(extra);w.save(self.root/'inbox'/(name+'.json'),value);return value

    def fake_commands(self):
        script=self.root/'fake_runner.py';script.write_text(FAKE_RUNNER)
        return patch.object(p,'command',side_effect=lambda item,res:[sys.executable,str(script),str(w.CODE),item['kind'],item['id'],res])

    def wait(self,children):
        for child in children:
            self.addCleanup(lambda c=child:c.kill() if c.poll() is None else None)
            result=child.wait(timeout=12)
            if result:
                logs='\n'.join(f.read_text()[-3000:] for f in (self.root/'logs').glob('*'))
                self.fail('Fake supervisor failed: '+logs)

    def test_all_thirty_eight_prs_are_dispatched_not_one_per_pass(self):
        for i in range(38):self.request('ado-'+str(i),100+i)
        self.request('teams-explicit',100,kind='teams')
        self.request('ado-draft',999,status='draft_hold')
        self.request('ado-self',998,status='self_authored')
        launched=[]
        with patch.object(p,'spawn',side_effect=lambda item:launched.append(item) or SimpleNamespace(pid=len(launched))):
            children=p.dispatch_round()
        self.assertEqual(len(children),38)
        self.assertEqual(len({x['pr_key'] for x in launched}),38)
        self.assertTrue(any(x['id']=='teams-explicit' for x in launched))

    def test_configured_concurrency_limit_is_respected(self):
        w.save(self.root/'scheduler-config.json',{'max_parallel_workers':2})
        for i in range(3):self.job('job-'+str(i),i+1)
        with patch.object(p,'spawn',return_value=SimpleNamespace(pid=1)) as spawn:
            self.assertEqual(len(p.dispatch_round()),2);self.assertEqual(spawn.call_count,2)

    def test_live_legacy_worker_is_preserved_and_counts_toward_capacity(self):
        self.job('legacy',42,status='active',worker_pid=os.getpid(),worker_identity=w.process_identity(os.getpid()))
        self.request('ado-duplicate',42);self.request('ado-other',43)
        with patch.object(p,'spawn',return_value=SimpleNamespace(pid=2)) as spawn:
            p.dispatch_round();self.assertEqual(spawn.call_count,1)
            self.assertEqual(spawn.call_args[0][0]['id'],'ado-other')

    def test_foreground_owner_is_not_taken_over(self):
        self.job('foreground',42,owner={'pid':os.getpid(),'identity':w.process_identity(os.getpid())})
        self.request('ado-duplicate',42)
        with patch.object(p,'spawn') as spawn:p.dispatch_round();spawn.assert_not_called()

    def test_ado_aliases_and_github_case_have_one_pipeline_key(self):
        self.assertEqual(p.pr_key(self.url(42)),p.pr_key('https://example-org.visualstudio.com/ProjectGuid/_git/RepoGuid/pullrequest/42'))
        self.assertEqual(p.pr_key('https://github.com/Azure/Repo/pull/42'),p.pr_key('https://github.com/azure/repo/pull/42'))

    def test_waiting_unchanged_pr_does_not_hot_poll(self):
        self.job('waiting',42,status='waiting',last_poll_at=w.utc())
        self.assertEqual(p.candidates(),[])

    def test_paused_blocked_and_done_jobs_are_not_dispatched(self):
        for i,state in enumerate(['paused','blocked','done']):self.job('job-'+str(i),i+1,status=state)
        self.assertEqual(p.candidates(),[])

    def test_unrelated_child_cannot_claim_a_reservation(self):
        job=self.job('job-one',42)
        w.save(p.record_path('job',job['id']),{'reservation':'expected','pid':os.getpid()+1})
        with self.assertRaises(ValueError):p.execute('job',job['id'],'expected')

    def test_workers_cannot_start_nested_dispatchers(self):
        with patch.dict(os.environ,{'AUTOWORKFLOW_JOB':'owned'}),patch.object(sys,'argv',['agent-workflow','tick']):
            with self.assertRaisesRegex(ValueError,'cannot start'):w.main()

    def test_real_supervisors_overlap_independent_pr_work(self):
        for i in range(3):self.job('job-'+str(i),i+1)
        with self.fake_commands(),patch.dict(os.environ,{'FIXTURE_READY':'3'}):
            children=p.dispatch_round();self.assertEqual(len(children),3);self.wait(children)
        starts=[float(f.read_text()) for f in (self.root/'markers').glob('*.start')]
        ends=[float(f.read_text()) for f in (self.root/'markers').glob('*.end')]
        self.assertEqual(len(starts),3);self.assertLess(max(starts),min(ends))
        self.assertEqual(len(w.recent_cycles()),3)

    def test_real_pr_lock_excludes_a_duplicate_even_if_it_bypasses_planner(self):
        first=self.job('job-one',42);second=self.job('job-two',42)
        with self.fake_commands(),patch.dict(os.environ,{'FIXTURE_READY':'2'}):
            children=[p.spawn({'kind':'job','id':j['id'],'url':j['url'],'pr_key':p.pr_key(j['url'])}) for j in [first,second]]
            self.wait(children)
        self.assertEqual(len(list((self.root/'markers').glob('*.start'))),1)
        self.assertEqual(len(w.recent_cycles()),1)

    def test_fast_preparation_starts_review_while_other_pr_is_still_preparing(self):
        self.request('ado-fast',41);self.request('ado-slow',42)
        with self.fake_commands(),patch.dict(os.environ,{'FIXTURE_PREPS':'2'}):
            children=p.dispatch_round();self.wait(children)
        markers=self.root/'markers'
        self.assertLess(float((markers/'ado-fast.start').read_text()),float((markers/'ado-slow.prep-end').read_text()))
        self.assertEqual(len(list(markers.glob('*.end'))),2)

    def test_parallel_budget_reservations_cannot_lose_or_exceed_usage(self):
        w.save(self.root/'scheduler-config.json',{'global_cycles_per_day':3})
        code='import sys;sys.path.insert(0,'+repr(str(w.CODE))+');import workflow;print(int(workflow.reserve_cycle()))'
        env={**os.environ,'AUTOWORKFLOW_ROOT':str(self.root)}
        children=[subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env) for _ in range(8)]
        values=[]
        for child in children:
            out,err=child.communicate(timeout=20);self.assertEqual(child.returncode,0,err);values.append(int(out))
        self.assertEqual(sum(values),3);self.assertEqual(len(w.recent_cycles()),3)

    def test_short_probe_retries_do_not_wait_for_the_next_five_minute_launch(self):
        now=time.time()
        self.job('waiting',42,status='waiting',probe_retry_at=now+30)
        self.assertTrue(p.short_retry_pending(now))
        job=w.load(w.job_path('waiting'));job['status']='paused';w.save(w.job_path('waiting'),job)
        self.assertFalse(p.short_retry_pending(now))

    def test_idle_dispatcher_keeps_only_short_pending_retries_alive(self):
        with patch.object(p,'dispatch_round',return_value=[]) as dispatch, \
             patch.object(p,'active',return_value=({},[],[],[])), \
             patch.object(p,'short_retry_pending',side_effect=[True,False]), \
             patch.object(p.time,'sleep') as sleep:
            p.run()
        self.assertEqual(dispatch.call_count,2)
        sleep.assert_called_once_with(5)

    def test_idle_dispatcher_without_work_stops_without_a_poll_loop(self):
        with patch.object(p,'dispatch_round',return_value=[]) as dispatch, \
             patch.object(p,'active',return_value=({},[],[],[])), \
             patch.object(p.time,'sleep') as sleep:
            p.run()
        dispatch.assert_called_once();sleep.assert_not_called()

    def test_invalid_scheduler_settings_fail_closed(self):
        w.save(self.root/'scheduler-config.json',{'max_parallel_workers':0})
        with self.assertRaises(ValueError):w.scheduler_settings()


from controller.fixtures import configured
ParallelDispatchTests.setUp = configured(ParallelDispatchTests.setUp)

if __name__=='__main__':unittest.main()
