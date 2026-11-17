"""Per-account GitHub CLI credentials without global account switching or token files."""
import json
import os
from pathlib import Path
import re
import subprocess
import time


class GitHubAuthError(RuntimeError):
    pass


class GitHubReadError(RuntimeError):
    pass


def workflow():
    import workflow as w
    return w


def native(name):
    import configuration
    try:return configuration.native(name)
    except FileNotFoundError:raise GitHubAuthError('Native '+name+' executable unavailable') from None


def state_path(login):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,80}',login):raise ValueError('Invalid configured GitHub login')
    return workflow().ROOT/'auth'/('github-'+login.lower()+'.json')


def current_login():
    result=subprocess.run([native('gh'),'api','user','--jq','.login'],capture_output=True,text=True,timeout=30)
    if result.returncode:raise GitHubAuthError('GitHub identity unavailable; use gh auth login in an interactive terminal')
    login=result.stdout.strip();state_path(login);return login


def failure(login):
    w=workflow();path=state_path(login)
    with w.lock('github-auth-'+login.lower()):
        state=w.load(path) if path.exists() else {'login':login,'affected':{}}
        previous=state.get('state');state.update(state='needs_sign_in',last_failure_at=w.utc())
        kind='job' if os.environ.get('AUTOWORKFLOW_JOB') else 'intake' if os.environ.get('AUTOWORKFLOW_INTAKE') else os.environ.get('AUTOWORKFLOW_AUTH_KIND')
        name=os.environ.get('AUTOWORKFLOW_JOB') or os.environ.get('AUTOWORKFLOW_INTAKE') or os.environ.get('AUTOWORKFLOW_AUTH_ID')
        if kind in ('job','intake') and name:
            w.job_path(name);state.setdefault('affected',{})[kind+':'+name]={'kind':kind,'id':name,'needs_resume':True}
        w.save(path,state)
        w.save(w.ROOT/'attention'/('github-auth-'+login.lower()+'.json'),{'provider':'github','state':'needs_sign_in','login':login,
            'updated_at':w.utc(),'steps':['Run gh auth login --hostname github.com interactively for '+login+'.',
            'Do not paste tokens into chat. The GitHub intake verifies the identity and resumes affected work after login.']})
        if previous!='needs_sign_in' or time.time()-state.get('last_notice',0)>=1800:
            w.notify('GitHub authentication needs help for '+login+'. Run gh auth login interactively; the watcher will verify recovery.')
            state['last_notice']=time.time();w.save(path,state)


def token_env(login,base=None):
    state_path(login)
    lookup_env={k:v for k,v in os.environ.items() if k not in ('GH_TOKEN','GITHUB_TOKEN')}
    result=subprocess.run([native('gh'),'auth','token','--hostname','github.com','--user',login],capture_output=True,text=True,timeout=15,env=lookup_env)
    token=result.stdout.strip()
    if result.returncode or not token or any(c.isspace() for c in token):
        failure(login);raise GitHubAuthError('GitHub stored login unavailable for '+login)
    env=dict(os.environ if base is None else base)
    env.pop('GITHUB_TOKEN', None)
    env.update(GH_TOKEN=token,GH_HOST='github.com')
    return env


def run_json(args,login):
    env=token_env(login)
    result=subprocess.run([native('gh'),*args],capture_output=True,text=True,env=env,timeout=45)
    if result.returncode:
        text=result.stderr.lower()
        if any(s in text for s in ['http 401','bad credentials','authentication required','gh auth login']):
            failure(login);raise GitHubAuthError('GitHub authentication unavailable for '+login)
        raise GitHubReadError('GitHub read unavailable; no empty-success assumption')
    try:data=json.loads(result.stdout)
    except ValueError:raise GitHubReadError('GitHub returned invalid JSON') from None
    if isinstance(data,dict) and data.get('errors'):raise GitHubReadError('GitHub API returned errors')
    return data


def verified(login,identity):
    if identity.get('login','').lower()!=login.lower() or not identity.get('id'):raise GitHubAuthError('GitHub authenticated identity mismatch')
    w=workflow();path=state_path(login)
    with w.lock('github-auth-'+login.lower()):
        state=w.load(path) if path.exists() else {'login':login,'affected':{}}
        if state.get('user_id') and state['user_id']!=identity['id']:raise GitHubAuthError('GitHub account ID changed; intake held')
        state.update(state='healthy',user_id=identity['id'],last_verified_at=w.utc());w.save(path,state)
        action=w.ROOT/'attention'/('github-auth-'+login.lower()+'.json')
        if action.exists():
            old=w.load(action);old.update(state='resolved',resolved_at=w.utc());w.save(action,old)
    for key,item in state.get('affected',{}).items():
        if not item.get('needs_resume'):continue
        lockname=('job-' if item['kind']=='job' else 'inbox-')+item['id']
        with w.lock(lockname,nonblocking=True) as acquired:
            if not acquired:continue
            target=w.job_path(item['id']) if item['kind']=='job' else w.ROOT/'inbox'/(item['id']+'.json')
            if not target.exists():continue
            with w.lock('registry'):
                current=w.load(target);pid=current.get('worker_pid');owner=current.get('owner') or {}
                if pid and w.process_identity(pid)==current.get('worker_identity'):continue
                if owner.get('pid') and w.process_identity(owner['pid'])==owner.get('identity'):continue
                if current['status'] in ('paused','done','self_authored','draft_hold','withdrawn'):continue
                if current['status']=='blocked' and not current.get('github_auth_hold'):continue
                current.update(status='ready' if item['kind']=='job' else 'pending',wake_at=time.time() if item['kind']=='job' else None,
                               next_retry_epoch=0,github_auth_hold=False,auth_provider=None)
                w.save(target,current)
            with w.lock('github-auth-'+login.lower()):
                latest=w.load(path);latest['affected'][key]['needs_resume']=False;w.save(path,latest)


def held(kind,name):
    for path in (workflow().ROOT/'auth').glob('github-*.json'):
        item=workflow().load(path).get('affected',{}).get(kind+':'+name)
        if item and item.get('needs_resume'):return True
    return False


def may_run(kind,name):
    w=workflow();path=w.job_path(name) if kind=='job' else w.ROOT/'inbox'/(name+'.json')
    if not path.exists():return True
    record=w.load(path);login=record.get('github_login')
    if not login:return True
    auth_path=state_path(login)
    if not auth_path.exists() or w.load(auth_path).get('state')=='healthy':return True
    with w.lock('github-auth-'+login.lower()):
        state=w.load(auth_path);state.setdefault('affected',{})[kind+':'+name]={'kind':kind,'id':name,'needs_resume':True};w.save(auth_path,state)
    return False


def hold_job(job):
    job.update(status='waiting',owner=None,github_auth_hold=True,auth_provider='github',wake_at=time.time()+300)


def health_tick():
    w = workflow()
    for path in (w.ROOT / 'auth').glob('github-*.json'):
        state = w.load(path)
        if state.get('state') == 'healthy' or time.time() - state.get('last_health_epoch', 0) < 300:
            continue
        state['last_health_epoch'] = time.time()
        w.save(path, state)
        try:
            verified(state['login'], run_json(['api', 'user'], state['login']))
        except (GitHubAuthError, GitHubReadError):
            continue


def worker_env(env,login):
    result=env.copy()
    result.pop('GH_TOKEN', None)
    result.pop('GITHUB_TOKEN', None)
    result.pop('AUTOWORKFLOW_GITHUB_LOGIN', None)
    if login:state_path(login);result['AUTOWORKFLOW_GITHUB_LOGIN']=login
    return result
