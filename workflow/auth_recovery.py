#!/usr/bin/env python3
"""Bounded ADO authentication recovery, human escalation, and automatic handback.

The controller never persists tokens, CLI output, or HTTP response bodies.
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid

RESOURCE='499b84ac-1321-427f-aa17-267ca6975798'
MAX_ATTEMPTS=3
DELAYS=(30,60,120)
CLI_TIMEOUT=12


class AuthError(RuntimeError):
    def __init__(self,reason='transient'):
        self.reason=reason
        super().__init__('ADO authentication '+reason+'; run agent-workflow auth-status')


def workflow():
    import workflow as w
    return w


def state_path():return workflow().ROOT/'auth'/'ado.json'
def attention_path():return workflow().ROOT/'attention'/'ado-auth.json'


def load_state():
    w=workflow()
    return w.load(state_path()) if state_path().exists() else {'provider':'ado','state':'healthy','attempts':0,'affected':{}}


def expected_account():
    w=workflow();path=w.ROOT/'ado-config.json'
    return w.load(path).get('account', '') if path.exists() else ''


def target(context=None):
    w=workflow()
    if context:return {k:context.get(k) for k in ('kind','id','url')}
    kind='job' if os.environ.get('AUTOWORKFLOW_JOB') else 'intake' if os.environ.get('AUTOWORKFLOW_INTAKE') else os.environ.get('AUTOWORKFLOW_AUTH_KIND')
    name=os.environ.get('AUTOWORKFLOW_JOB') or os.environ.get('AUTOWORKFLOW_INTAKE') or os.environ.get('AUTOWORKFLOW_AUTH_ID')
    if kind not in ('job','intake') or not name:return None
    w.job_path(name)
    path=w.job_path(name) if kind=='job' else w.ROOT/'inbox'/(name+'.json')
    url=w.load(path).get('url') if path.exists() else os.environ.get('AUTOWORKFLOW_INTAKE_URL')
    return {'kind':kind,'id':name,'url':url}


def _remember(state,context,reason):
    item=target(context)
    if not item:return
    if item.get('kind') not in ('job','intake'):return
    w=workflow();w.job_path(item['id']);key=item['kind']+':'+item['id']
    old=state.setdefault('affected',{}).get(key,{})
    in_model=bool(os.environ.get('AUTOWORKFLOW_JOB') or os.environ.get('AUTOWORKFLOW_INTAKE'))
    required=reason!='busy' or in_model or old.get('resume_required',False)
    state['affected'][key]={**old,**item,'reason':reason,'last_failure_at':w.utc(),
        'failure_sequence':old.get('failure_sequence',0)+1,'failure_epoch':time.time(),
        'phase':'model' if in_model or (old.get('needs_resume') and old.get('phase')=='model') else 'probe',
        'needs_resume':required,'resume_required':required}


def _attention(state):
    w=workflow()
    needs=state.get('state') in ('needs_sign_in','identity_mismatch')
    if not needs:return
    action={'provider':'ado','state':state['state'],'episode':state['episode'],'updated_at':w.utc(),
        'reason':state.get('reason'),'account':expected_account(),'affected':list(state.get('affected',{}).values()),
        'steps':['If Azure CLI timed out, restore network/GSA access and verify az account show first.',
                 'If sign-in expired or MFA is required, run az login interactively with the existing work account and tenant.',
                 'Complete browser/MFA prompts. Do not paste credentials or tokens into chat.',
                 'Run agent-workflow auth-check. The controller also detects recovery automatically.'],
        'commands':['az account show','az login','agent-workflow auth-check'],'automatic_resume':True}
    w.save(attention_path(),action)
    if state.get('notified_episode')!=state['episode'] or time.time()-state.get('last_notification_epoch',0)>=1800:
        accepted=w.notify('ADO authentication needs help ('+str(state.get('reason'))+'). See agent-workflow auth-status; restore CLI/sign-in, then auth-check. Affected PRs: '+str(len(state.get('affected',{}))))
        state.update(notified_episode=state['episode'],last_notification_epoch=time.time(),notification_request_accepted=bool(accepted))


def failure(reason='transient',context=None):
    w=workflow()
    with w.lock('auth-state'):
        state=load_state();now=time.time()
        if reason in ('busy','deferred'):
            _remember(state,context,reason);w.save(state_path(),state);return state
        if state.get('state')=='healthy':
            state.update(episode=uuid.uuid4().hex,attempts=0,first_failure_at=w.utc())
        state.setdefault('episode',uuid.uuid4().hex)
        state['attempts']=min(state.get('attempts',0)+1,MAX_ATTEMPTS)
        human=reason in ('sign_in','identity_mismatch') or state['attempts']>=MAX_ATTEMPTS
        state.update(state=('identity_mismatch' if reason=='identity_mismatch' else 'needs_sign_in') if human else 'retrying',
            reason=reason,last_failure_at=w.utc(),next_probe_epoch=now+(300 if human else DELAYS[state['attempts']-1]))
        _remember(state,context,reason);_attention(state);w.save(state_path(),state)
        return state


def permission_failure(context=None,resource_url=None):
    w=workflow();item=target(context)
    if not item:return failure('identity_mismatch',context)
    if resource_url and not resource_url.startswith('https://dev.azure.com/'):
        raise ValueError('Refusing an unscoped permission probe')
    with w.lock('auth-state'):
        state=load_state();_remember(state,item,'permission')
        key=item['kind']+':'+item['id'];entry=state['affected'][key]
        entry.update(permission_pending=True,resource_url=resource_url)
        path=w.ROOT/'attention'/('ado-access-'+item['id']+'.json')
        w.save(path,{'provider':'ado','state':'needs_access','updated_at':w.utc(),'target':item,
            'steps':['Verify that the existing work account has access to this PR/resource.',
                     'Restore the required access through the normal owner/admin process; no automatic role grant is attempted.',
                     'Run agent-workflow auth-check. Read access is rechecked before resumption.']})
        if not entry.get('permission_notified'):
            w.notify('ADO access denied for '+item['id']+'. See agent-workflow auth-status; restore access, then run auth-check.')
            entry['permission_notified']=True
        w.save(state_path(),state)
    return state


def http_failure(error,context=None,resource_url=None):
    if error.code==401:
        failure('unauthorized',context);raise AuthError('unauthorized') from None
    if error.code==403:
        permission_failure(context,resource_url);raise AuthError('permission') from None
    raise error


def recovered(verified=True):
    w=workflow()
    with w.lock('auth-state'):
        state=load_state();was_unhealthy=state.get('state')!='healthy'
        state.update(state='healthy',attempts=0,last_success_at=w.utc(),next_probe_epoch=None,reason=None)
        if verified:state['verified_epoch']=time.time()
        if was_unhealthy:state['recovered_at']=w.utc()
        w.save(state_path(),state)
        if attention_path().exists():
            action=w.load(attention_path());action.update(state='resolved',resolved_at=w.utc());w.save(attention_path(),action)
    if was_unhealthy:w.notify('ADO authentication recovered. Affected PR jobs will resume with fresh verification.')


def classify_cli(error):
    if isinstance(error,subprocess.TimeoutExpired):return 'timeout'
    text=str(getattr(error,'stderr','') or '').lower()
    if any(s in text for s in ['az login','interaction_required','aadsts50076','aadsts50079','aadsts700082','aadsts50173','refresh token has expired','login required']):return 'sign_in'
    return 'transient'


def real_azure_cli():
    # Use installed tools, not copied credentials or a background browser login.
    import configuration
    try:
        return configuration.native('az')
    except FileNotFoundError:
        raise AuthError('cli_missing') from None


def _raw_token(skill):
    import probes
    real=subprocess;reason=['transient']
    class BoundedCLI:
        def __getattr__(self,name):return getattr(real,name)
        def run(self,args,**kwargs):
            args=list(args)
            if args[:3]==['az','account','get-access-token']:
                args[0]=real_azure_cli();kwargs['timeout']=min(kwargs.get('timeout',CLI_TIMEOUT),CLI_TIMEOUT)
            try:return real.run(args,**kwargs)
            except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as error:
                reason[0]=classify_cli(error);raise
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return probes.clean_ado_token(skill.get_token())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise AuthError(classify_cli(error)) from None
    except (SystemExit, RuntimeError, FileNotFoundError):
        raise AuthError('transient') from None


def verify_token(token):
    w=workflow();path=w.ROOT/'ado-config.json';config=w.load(path) if path.exists() else {}
    organizations = config.get('organizations') or []
    if not organizations or not expected_account():
        raise AuthError('identity_mismatch')
    organization = organizations[0]
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,62}',organization):raise AuthError('identity_mismatch')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    request=urllib.request.Request('https://dev.azure.com/'+organization+'/_apis/connectionData?api-version=7.1-preview.1',headers={'Authorization':'Bearer '+token,'Accept':'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request,timeout=10) as response:data=json.load(response)
    except urllib.error.HTTPError as error:
        raise AuthError('sign_in' if error.code==401 else 'identity_mismatch' if error.code==403 else 'transient') from None
    except (OSError,ValueError):raise AuthError('transient') from None
    user=data.get('authenticatedUser') or {}
    account=((user.get('properties') or {}).get('Account') or {}).get('$value','')
    if account.lower()!=expected_account().lower() or not user.get('id'):raise AuthError('identity_mismatch')
    with w.lock('auth-state'):
        state=load_state();expected_id=state.get('expected_identity_id')
        if expected_id and expected_id!=user['id']:raise AuthError('identity_mismatch')
        if not expected_id:
            state['expected_identity_id']=user['id'];w.save(state_path(),state)
    return {'account':account,'id':user['id']}


def acquire(skill,context=None,force=False):
    w=workflow();state=load_state();now=time.time()
    if not force and state.get('state')!='healthy':
        if state.get('state') in ('needs_sign_in','identity_mismatch') or state.get('next_probe_epoch',0)>now:
            failure('deferred',context);raise AuthError('deferred')
    # Contention must not cause dozens of Azure CLI processes or an unbounded wait.
    deadline=time.monotonic()+6
    while True:
        with w.lock('ado-auth',nonblocking=True) as acquired:
            if acquired:
                state=load_state()
                if not force and state.get('state')!='healthy' and (state.get('state') in ('needs_sign_in','identity_mismatch') or (state.get('next_probe_epoch') or 0)>time.time()):
                    failure('deferred',context);raise AuthError('deferred')
                try:
                    token=_raw_token(skill)
                    verified=(force or state.get('state')!='healthy' or not state.get('expected_identity_id')
                              or now-state.get('verified_epoch', 0)>=300)
                    if verified:verify_token(token)
                except AuthError as error:
                    failure(error.reason,context);raise
                recovered(verified=verified);return token
        if time.monotonic()>=deadline:
            failure('busy',context);raise AuthError('busy')
        time.sleep(.05)


def load_skill():
    import ado
    return ado


def held(kind,name):
    item=load_state().get('affected',{}).get(kind+':'+name)
    return item if item and item.get('needs_resume') else None


def credit_cycle(job,timestamp):
    # Keep total usage for the global guard and audit; an auth-only interruption
    # must not consume the PR's ordinary review/repair retry allowance.
    values=job.setdefault('auth_cycle_times',[])
    if timestamp not in values:values.append(timestamp)


def hold_job(job,model=False):
    w=workflow();state=load_state()
    if not job.get('auth_hold'):
        job['auth_resume_status']='ready' if model or job.get('status') in ('active','ready') or job.get('pending_action') else 'waiting'
        job['auth_resume_wake_at']=job.get('wake_at')
    job.update(status='waiting',owner=None,auth_hold=True,auth_reason=state.get('reason') or 'deferred',
               wake_at=state.get('next_probe_epoch') or time.time()+60,updated_at=w.utc())
    return job


def may_run(kind,name):
    state=load_state();item=state.get('affected',{}).get(kind+':'+name)
    if item and item.get('permission_pending'):return False
    if state.get('state')!='healthy':
        if item and item.get('needs_resume'):return False
        w=workflow();path=w.job_path(name) if kind=='job' else w.ROOT/'inbox'/(name+'.json')
        if path.exists():
            record=w.load(path);url=record.get('url') or ''
            if url.startswith('https://dev.azure.com/') or '.visualstudio.com/' in url:
                failure('deferred',{'kind':kind,'id':name,'url':url});return False
    return True


def reconcile():
    w=workflow();state=load_state()
    if state.get('state')!='healthy':return []
    resumed=[]
    for key,item in state.get('affected',{}).items():
        if not item.get('needs_resume') or item.get('permission_pending'):continue
        if item.get('failure_epoch',0)>state.get('verified_epoch',0):continue
        kind,name=item['kind'],item['id'];lockname=('job-' if kind=='job' else 'inbox-')+name
        with w.lock(lockname,nonblocking=True) as acquired:
            if not acquired:continue
            path=w.job_path(name) if kind=='job' else w.ROOT/'inbox'/(name+'.json')
            if not path.exists():continue
            with w.lock('registry'):
                current=w.load(path)
                pid=current.get('worker_pid');owner=current.get('owner') or {}
                if pid and w.process_identity(pid)==current.get('worker_identity'):continue
                if owner.get('pid') and w.process_identity(owner['pid'])==owner.get('identity'):continue
                terminal=current.get('status') in ('paused','done','terminal','withdrawn','self_authored','draft_hold','existing_watch')
                if terminal:pass
                elif kind=='job' and (current['status']!='blocked' or current.get('auth_hold')):
                    restore=current.pop('auth_resume_status',None)
                    pending_model=item.get('phase')=='model' or bool(current.get('pending_action'))
                    if restore is None:restore='waiting' if item.get('phase')=='probe' and current['status']=='waiting' and not pending_model else 'ready'
                    if pending_model:restore='ready'
                    original_wake=current.pop('auth_resume_wake_at',None)
                    current.update(status=restore,owner=None,wake_at=time.time() if restore=='ready' else original_wake,
                                   auth_hold=False,auth_resumed_at=w.utc())
                    if restore=='waiting':current['probe_retry_at']=time.time()
                    current.pop('probe_error',None);w.save(path,current);resumed.append(item)
                elif kind=='intake' and current['status'] in ('auth_wait','pending','preparing'):
                    current.update(status='pending',worker_pid=None,worker_identity=None,next_retry_epoch=0,auth_hold=False,
                                   last_error=None,decision=None,auth_resumed_at=w.utc());w.save(path,current);resumed.append(item)
                elif kind=='intake' and current['status']=='linked':pass
                else:
                    # Do not silently override an unrelated safety/design hold.
                    action_path=w.ROOT/'attention'/('ado-resume-'+name+'.json')
                    if not action_path.exists():
                        w.save(action_path,{'state':'needs_job_decision','target':item,'updated_at':w.utc(),
                            'reason':'Authentication recovered, but the job retains a separate blocker.',
                            'steps':['Inspect the existing job decision. Do not waive scope or safety restrictions.',
                                     'Use agent-workflow resume '+name+' only after that blocker is resolved.']})
                        w.notify('ADO auth recovered, but '+name+' retains a separate blocker. Inspect its decision before resuming.')
                    continue
            with w.lock('auth-state'):
                latest=load_state();entry=latest.get('affected',{}).get(key,{})
                if entry.get('failure_sequence')==item.get('failure_sequence'):
                    entry.update(needs_resume=False,resume_required=False,resumed_at=w.utc());w.save(state_path(),latest)
    return resumed


def check_permissions(token):
    w=workflow();state=load_state()
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    opener=urllib.request.build_opener(NoRedirect())
    for key,item in state.get('affected',{}).items():
        url=item.get('resource_url')
        if not item.get('permission_pending') or not url:continue
        request=urllib.request.Request(url,headers={'Authorization':'Bearer '+token,'Accept':'application/json'})
        try:
            with opener.open(request,timeout=10) as response:
                if response.status!=200:continue
                json.load(response)
        except (OSError,ValueError):continue
        with w.lock('auth-state'):
            latest=load_state();entry=latest.get('affected',{}).get(key,{})
            if entry.get('failure_sequence')!=item.get('failure_sequence'):continue
            entry['permission_pending']=False;w.save(state_path(),latest)
            path=w.ROOT/'attention'/('ado-access-'+item['id']+'.json')
            if path.exists():
                action=w.load(path);action.update(state='resolved',resolved_at=w.utc());w.save(path,action)


def legacy_sweep():
    """Recover older, completed workers that predate the managed CLI adapter."""
    w=workflow();state=load_state();seen=state.get('legacy_scanned',{})
    for job in w.jobs():
        if 'dev.azure.com/' not in job.get('url','') and '.visualstudio.com/' not in job.get('url',''):continue
        marker=job.get('last_checkpoint_cycle') or job.get('last_worker_log')
        if not marker or seen.get('job:'+job['id'])==marker:continue
        next_action=job.get('next_action','')
        if job['status']!='waiting' or job.get('wake_at') or job.get('pending_action'):continue
        if not re.search(r'\b(?:authentication|auth|token)\b.{0,80}\b(?:recover|recovers|recovered|recovery|restore|restored|unavailable|failed|failure|timeout|timed out)\b',next_action,re.I):continue
        if not re.search(r'\b(?:post|comment|publish|approve)\b',next_action,re.I):continue
        pid=job.get('worker_pid')
        if pid and w.process_identity(pid)==job.get('worker_identity'):continue
        with w.lock('job-'+job['id'],nonblocking=True) as acquired:
            if not acquired:continue
            current=w.load(w.job_path(job['id']))
            if current['status']!='waiting' or current.get('owner') or current.get('worker_pid'):continue
            current['pending_action']='comment' if re.search(r'\b(?:post|comment)\b',next_action,re.I) else 'approval'
            w.save(w.job_path(job['id']),current)
        failure('deferred',{'kind':'job','id':job['id'],'url':job['url']})
        with w.lock('auth-state'):
            latest=load_state();latest.setdefault('legacy_scanned',{})['job:'+job['id']]=marker;w.save(state_path(),latest)
    for path in (w.ROOT/'inbox').glob('*.json'):
        request=w.load(path);marker=request.get('last_log')
        key='intake:'+request.get('id','')
        if request.get('status')!='blocked' or not marker or seen.get(key)==marker:continue
        log=Path(marker)
        if (w.ROOT/'logs').resolve() not in log.resolve().parents or not log.is_file():continue
        # Read only the bounded final summary. Never persist its contents.
        text=log.read_text(errors='replace')[-16000:]
        if not re.search(r'Azure CLI token (?:request|acquisition) timed out',text,re.I):continue
        pid=request.get('worker_pid')
        if pid and w.process_identity(pid)==request.get('worker_identity'):continue
        with w.lock('inbox-'+request['id'],nonblocking=True) as acquired:
            if not acquired:continue
            current=w.load(path)
            if current.get('status')!='blocked':continue
            current.update(status='auth_wait',auth_hold=True,auth_retry_count=current.get('auth_retry_count',0)+1)
            w.save(path,current)
        failure('deferred',{'kind':'intake','id':request['id'],'url':request.get('url')})
        with w.lock('auth-state'):
            latest=load_state();latest.setdefault('legacy_scanned',{})[key]=marker;w.save(state_path(),latest)


def tick(force=False):
    w=workflow();legacy_sweep();state=load_state()
    verification_due=state.get('state')=='healthy' and any(item.get('needs_resume') and item.get('failure_epoch',0)>state.get('verified_epoch',0) for item in state.get('affected',{}).values())
    permission_due=any(item.get('permission_pending') for item in state.get('affected',{}).values()) and time.time()-state.get('last_permission_probe',0)>=300
    verification_performed=False;check_error=None
    if force or verification_due or permission_due or (state.get('state')!='healthy' and time.time()>=(state.get('next_probe_epoch') or 0)):
        try:
            token=acquire(load_skill(),force=True)
            verification_performed=True
            check_permissions(token)
            if permission_due:
                with w.lock('auth-state'):
                    latest=load_state();latest['last_permission_probe']=time.time();w.save(state_path(),latest)
        except AuthError as error:check_error=error.reason
    with w.lock('auth-state'):
        state=load_state()
        if state.get('state') in ('needs_sign_in','identity_mismatch'):_attention(state);w.save(state_path(),state)
    resumed=reconcile()
    return {'state':load_state(),'verification_performed':verification_performed,'check_error':check_error,
            'resumed':[{'kind':i['kind'],'id':i['id'],'url':i.get('url')} for i in resumed]}


def status():
    w=workflow();state=load_state()
    return {'provider':'ado','state':state.get('state'),'attempts':state.get('attempts',0),'reason':state.get('reason'),
        'last_success_at':state.get('last_success_at'),'verified_epoch':state.get('verified_epoch'),
        'expected_identity_id':state.get('expected_identity_id'),'next_probe_epoch':state.get('next_probe_epoch'),
        'affected':[v for v in state.get('affected',{}).values() if v.get('needs_resume')],
        'human_action':w.load(attention_path()) if attention_path().exists() else None,
        'action_required':[w.load(path) for path in (w.ROOT/'attention').glob('ado-*.json') if w.load(path).get('state')!='resolved']}


def worker_env(env):
    import configuration
    result=configuration.clean_worker_env(env)
    result['PATH']=str(workflow().CODE/'bin')+os.pathsep+result.get('PATH',os.defpath)
    result['AUTOWORKFLOW_ROOT']=str(workflow().ROOT)
    result['AUTOWORKFLOW_WORKIQ']=str(workflow().CODE/'bin/teams-reader')
    result['AUTOWORKFLOW_TEAMS_READER']=str(workflow().CODE/'bin/teams-reader')
    return result


def main():
    os.umask(0o077);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['tick','status','check']);args=parser.parse_args()
    result=status() if args.command=='status' else tick(force=args.command=='check')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
