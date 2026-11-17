"""Concurrent per-PR supervisors. One pipeline lock covers preparation and review."""
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.parse
import uuid

import workflow as w


def pr_key(url):
    url=w.canonical_url(url);parsed=urllib.parse.urlsplit(url);parts=parsed.path.strip('/').split('/')
    if parsed.hostname=='dev.azure.com':
        # ADO PR IDs are collection-wide. Names and GUID URL aliases share a key.
        identity=['ado',parts[0].lower(),str(int(parts[-1]))]
    else:identity=['github',parts[0].lower(),parts[1].lower(),str(int(parts[-1]))]
    return w.digest(identity)


def record_path(kind,name):
    w.job_path(name)
    if kind not in ('job','intake'):raise ValueError('Unknown pipeline kind')
    return w.ROOT/'dispatch'/(kind+'-'+name+'.json')


def records():return [w.load(p) for p in (w.ROOT/'dispatch').glob('*.json')]


def live(pid,identity,cache=None):
    if not pid or not identity:return False
    cache={} if cache is None else cache
    if pid not in cache:cache[pid]=w.process_identity(pid)
    return bool(cache[pid] and cache[pid]==identity)


def intake_rows():
    return [w.load(p) for p in (w.ROOT/'inbox').glob('*.json') if p.name.startswith(('teams-','ado-','github-'))]


def active(cache=None):
    cache={} if cache is None else cache
    pipelines={};supervisors=[];model_workers=[];foreground=[]
    for r in records():
        if r.get('state') in ('starting','running') and live(r.get('pid'),r.get('identity'),cache):
            pipelines[r['pr_key']]=r;supervisors.append(r)
    for j in w.jobs():
        if live(j.get('worker_pid'),j.get('worker_identity'),cache):
            item={'id':j['id'],'kind':'review_or_repair','pid':j['worker_pid'],'url':j['url'],'pr_key':pr_key(j['url'])}
            pipelines[item['pr_key']]=item;model_workers.append(item)
        owner=j.get('owner') or {}
        if live(owner.get('pid'),owner.get('identity'),cache):
            item={'id':j['id'],'kind':'foreground','pid':owner['pid'],'url':j['url'],'pr_key':pr_key(j['url'])}
            pipelines[item['pr_key']]=item;foreground.append(item)
    for r in intake_rows():
        if r.get('url') and live(r.get('worker_pid'),r.get('worker_identity'),cache):
            item={'id':r['id'],'kind':'preparation','pid':r['worker_pid'],'url':r['url'],'pr_key':pr_key(r['url'])}
            pipelines[item['pr_key']]=item;model_workers.append(item)
    return pipelines,supervisors,model_workers,foreground


def epoch(value):
    if not value:return 0
    try:return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError):return 0


def candidates(now=None):
    now=time.time() if now is None else now
    settings=w.scheduler_settings();items=[]
    for j in w.jobs():
        if j['status'] in ('paused','done','blocked'):continue
        import auth_recovery
        if not auth_recovery.may_run('job',j['id']):continue
        import github_auth
        if not github_auth.may_run('job',j['id']):continue
        if j.get('probe_retry_at',0)>now:continue
        age=now-epoch(j.get('last_poll_at'))
        due=bool((j.get('wake_at') and j['wake_at']<=now) or (j.get('probe_retry_at') and j['probe_retry_at']<=now))
        model_ready=j['status'] in ('ready','active') and not (j.get('budget_wait_until',0)>now)
        interval=settings['github_repository_probe_interval'] if j.get('github_repository_watch') else settings['probe_interval']
        if not model_ready and not due and age<interval:continue
        items.append({'kind':'job','id':j['id'],'url':j['url'],'pr_key':pr_key(j['url']),'order':(0,epoch(j.get('last_poll_at')),j['id'])})
    if len(w.recent_cycles())<w.global_cycle_limit():
        for r in intake_rows():
            if r.get('status') not in ('pending','preparing') or not r.get('url') or r.get('next_retry_epoch',0)>now:continue
            import auth_recovery
            if not auth_recovery.may_run('intake',r['id']):continue
            import github_auth
            if not github_auth.may_run('intake',r['id']):continue
            adapter=w.intake_adapter(r)
            if not adapter.config_path().exists():continue
            config=w.load(adapter.config_path())
            if not config.get('enabled') or not config.get('auto_start_reviews'):continue
            priority=1 if r.get('source_kind','teams')=='teams' else 2
            items.append({'kind':'intake','id':r['id'],'url':r['url'],'pr_key':pr_key(r['url']),
                          'order':(priority,epoch(r.get('created_at')),r['id'])})
    # Prefer an enrolled job, then an explicit Teams request, over its ADO duplicate.
    result=[];seen=set()
    for item in sorted(items,key=lambda v:v['order']):
        if item['pr_key'] not in seen:result.append(item);seen.add(item['pr_key'])
    return result


def command(item,reservation):
    return [sys.executable,str(w.CODE/'workflow.py'),'_cycle',item['kind'],item['id'],'--reservation',reservation]


def spawn(item):
    path=record_path(item['kind'],item['id']);reservation=uuid.uuid4().hex
    import auth_recovery
    env=auth_recovery.worker_env(os.environ.copy())
    for key in ('AUTOWORKFLOW_JOB','AUTOWORKFLOW_CYCLE','AUTOWORKFLOW_INTAKE','AUTOWORKFLOW_INTAKE_URL',
                'PI_SESSION_ID','PI_SESSION_FILE','PI_TERM_NAME','CLAUDE_SESSION_ID','CLAUDE_TERM_NAME'):
        env.pop(key,None)
    env['AUTOWORKFLOW_ROOT']=str(w.ROOT)
    env.update(AUTOWORKFLOW_AUTH_KIND=item['kind'],AUTOWORKFLOW_AUTH_ID=item['id'])
    log_path=w.ROOT/'logs'/(item['id']+'-supervisor-'+reservation+'.log');log_path.parent.mkdir(parents=True,exist_ok=True)
    # Publish the PID while holding the registry lock. The child takes the same
    # lock before it validates its reservation, so a fast child cannot race this.
    with w.lock('registry'):
        record={k:item[k] for k in ('kind','id','url','pr_key')}
        record.update(reservation=reservation,state='starting',created_at=w.utc(),started_epoch=time.time(),log=str(log_path))
        w.save(path,record)
        try:
            with log_path.open('w') as log:
                proc=subprocess.Popen(command(item,reservation),cwd=str(Path.home()),env=env,stdin=subprocess.DEVNULL,
                    stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            record.update(pid=proc.pid,identity=w.process_identity(proc.pid))
            w.save(path,record)
        except Exception:
            record.update(state='spawn_failed',finished_at=w.utc());w.save(path,record);raise
    return proc


def dispatch_round():
    import auth_recovery
    auth_recovery.reconcile()
    import teams_reader
    teams_reader.reconcile()
    now=time.time();cache={};running,_,_,_=active(cache)
    capacity=w.scheduler_settings()['max_parallel_workers']-len(running)
    if capacity<=0:return []
    previous={(r['kind'],r['id']):r for r in records()}
    launched=[]
    for item in candidates(now):
        if len(launched)>=capacity:break
        if item['pr_key'] in running:continue
        last=previous.get((item['kind'],item['id']),{})
        # Verification/provider failures must not spawn a hot process loop.
        if last.get('state') in ('exited','spawn_failed') and now-last.get('finished_epoch',0)<30:continue
        proc=spawn(item);launched.append(proc);running[item['pr_key']]=item
    return launched


def execute(kind,name,reservation):
    path=record_path(kind,name)
    with w.lock('registry'):
        record=w.load(path)
        if record.get('reservation')!=reservation or record.get('pid')!=os.getpid():
            raise ValueError('Supervisor reservation does not belong to this process')
    error=None
    try:
        with w.lock('pipeline-'+record['pr_key'],nonblocking=True) as acquired:
            if not acquired:return
            with w.lock('registry'):
                current=w.load(path);current.update(state='running');w.save(path,current)
            if kind=='job':
                job=w.load(w.job_path(name))
                if pr_key(job['url'])!=record['pr_key']:raise ValueError('Job routing changed')
                w.tick_one(name)
            else:
                request=w.load(w.ROOT/'inbox'/(name+'.json'))
                if pr_key(request['url'])!=record['pr_key']:raise ValueError('Intake routing changed')
                w.intake_adapter(request).prepare_one(name)
                # Preparation and its initial substantive review are one PR
                # pipeline. Other PRs do not wait for either stage to finish.
                linked=next((j for j in w.jobs() if j.get('intake')==name and j['mode']=='review'
                             and pr_key(j['url'])==record['pr_key'] and j['status'] in ('ready','waiting')),None)
                if linked:w.tick_one(linked['id'])
    except (Exception,SystemExit) as exc:
        error=type(exc).__name__
    finally:
        with w.lock('registry'):
            current=w.load(path)
            if current.get('reservation')==reservation:
                current.update(state='exited',finished_at=w.utc(),finished_epoch=time.time(),error=error)
                w.save(path,current)
    if error:raise RuntimeError('PR supervisor failed ('+error+'); inspect its private log')


def status():
    running,supervisors,models,foreground=active()
    return {'settings':w.scheduler_settings(),'active_pr_pipelines':len(running),
            'supervisors':[{k:r.get(k) for k in ('kind','id','url','pid','state','created_at')} for r in supervisors],
            'model_workers':models,'foreground_owners':foreground,
            'eligible_unique_pipelines':sum(item['pr_key'] not in running for item in candidates())}


def short_retry_pending(now=None):
    """Keep short deferred probes within this existing dispatcher."""
    now=time.time() if now is None else now
    import auth_recovery
    state=auth_recovery.load_state()
    if state.get('state')!='healthy':return False
    for job in w.jobs():
        if job['status'] in ('paused','done','blocked'):continue
        affected=state.get('affected',{}).get('job:'+job['id'],{})
        if affected.get('permission_pending'):continue
        retry=job.get('probe_retry_at')
        if retry and 0<retry-now<=60:return True
    previous={(r['kind'],r['id']):r for r in records()}
    for item in candidates(now):
        last=previous.get((item['kind'],item['id']),{})
        if last.get('state') in ('exited','spawn_failed') and now-last.get('finished_epoch',0)<30:
            return True
    return False


def run():
    # One dispatcher coordinates all enrolled local PR pipelines.
    with w.lock('parallel-dispatcher',nonblocking=True) as acquired:
        if not acquired:return
        children=[]
        while True:
            children=[p for p in children if p.poll() is None]
            children.extend(dispatch_round())
            if not active()[0] and not short_retry_pending():return
            time.sleep(w.scheduler_settings()['dispatch_interval'])
