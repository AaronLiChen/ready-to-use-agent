#!/usr/bin/env python3
"""Direct GitHub review requests and explicitly authorized repository-wide intake."""
from collections import Counter
import json
from pathlib import Path
import re
import time
import urllib.parse

import workflow as w
import github_auth
import parallel_dispatch
import teams_intake


def config_path():return w.ROOT/'github-config.json'
def state_path():return w.ROOT/'github-state.json'


class GitHubClient:
    def __init__(self,login):
        self.login=login
        self.identity=github_auth.run_json(['api','user'],login)
        github_auth.verified(login,self.identity)

    def get(self,path):
        if path.startswith(('/', 'http:','https:')) or '..' in path.split('/'):
            raise ValueError('Refusing an unscoped GitHub endpoint')
        return github_auth.run_json(['api',path],self.login)

    def requested(self):
        query='is:pr is:open user-review-requested:'+self.login
        values=[];seen=set()
        for page in range(1,11):
            data=self.get('search/issues?'+urllib.parse.urlencode({'q':query,'per_page':100,'page':page}))
            if data.get('incomplete_results') or data.get('total_count',0)>1000:
                raise RuntimeError('GitHub search incomplete or over its 1000-result bound; coverage not complete')
            items=data.get('items')
            if not isinstance(items,list):raise RuntimeError('Missing GitHub review-request collection')
            signature=w.digest([item.get('id') for item in items])
            if items and signature in seen:raise RuntimeError('GitHub pagination did not advance')
            seen.add(signature);values.extend(items)
            if len(items)<100 or len(values)>=data.get('total_count',0):return values
        raise RuntimeError('GitHub review-request pagination incomplete')

    def repository_prs(self,repository):
        repository_name(repository)
        values=[];seen=set()
        for page in range(1,1001):
            query=urllib.parse.urlencode({'state':'open','per_page':100,'page':page,'sort':'created','direction':'asc'})
            items=self.get('repos/'+repository+'/pulls?'+query)
            if not isinstance(items,list):raise RuntimeError('Missing repository PR collection; coverage incomplete')
            signature=w.digest([item.get('id') for item in items])
            if items and signature in seen:raise RuntimeError('Repository PR pagination did not advance')
            seen.add(signature);values.extend(items)
            if len(items)<100:return values
        raise RuntimeError('Repository PR pagination exceeded its bound; coverage incomplete')


def repository_name(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+',value):
        raise ValueError('A repository watch requires an owner/repository name')
    if any(part in ('.','..') for part in value.split('/')):raise ValueError('Invalid repository name')
    return value


def repository_watches(config):
    entries=config.get('repository_watches',[])
    if not isinstance(entries,list):raise ValueError('Repository watches must contain an array')
    own={login.lower():login for login in config.get('logins',[]) if isinstance(login,str)}
    result={}
    for entry in entries:
        if not isinstance(entry,dict) or not isinstance(entry.get('enabled',True),bool):
            raise ValueError('Invalid repository-watch configuration')
        if not entry.get('enabled',True):continue
        repository=repository_name(entry.get('repository'));login=entry.get('login')
        if not isinstance(login,str) or login.lower() not in own:
            raise ValueError('Pin each repository watch to a configured GitHub login')
        key=repository.lower();login=own[login.lower()]
        if key in result and result[key]['login'].lower()!=login.lower():
            raise ValueError('One repository watch must use one account for this human')
        result[key]={'repository':repository,'login':login,'enabled':True}
    return list(result.values())


def repository_for_url(url):
    api_path(url)
    parts=urllib.parse.urlsplit(w.canonical_url(url)).path.strip('/').split('/')
    return '/'.join(parts[:2])


def watch_for(config,url,login):
    repository=repository_for_url(url).lower()
    return next((entry for entry in repository_watches(config)
                 if entry['repository'].lower()==repository and entry['login'].lower()==login.lower()),None)


def api_path(url):
    parsed=urllib.parse.urlsplit(w.canonical_url(url));parts=parsed.path.strip('/').split('/')
    if parsed.hostname!='github.com' or len(parts)!=4:raise ValueError('Not a GitHub PR URL')
    return 'repos/'+parts[0]+'/'+parts[1]+'/pulls/'+str(int(parts[3]))


def direct(pr,login):
    return any(user.get('login','').lower()==login.lower() for user in pr.get('requested_reviewers',[]))


def record(config,client,pr):
    if pr.get('state')!='open':return None
    url=w.canonical_url(pr['html_url']);watch=watch_for(config,url,client.login)
    requested=direct(pr,client.login)
    if not requested and not watch:return None
    if not pr.get('user',{}).get('login'):raise RuntimeError('GitHub PR author is unavailable')
    key_value=parallel_dispatch.pr_key(url)
    history=[j for j in w.jobs() if parallel_dispatch.pr_key(j['url'])==key_value]
    active=next((j for j in history if j['status']!='done'),None)
    generation=sum(j['status']=='done' for j in history)
    key='github-'+w.digest(url.lower())[:20]+('-r'+str(generation) if generation and not active else '')
    if active and str(active.get('intake','')).startswith('github-'):key=active['intake']
    path=w.ROOT/'inbox'/(key+'.json')
    with w.lock('inbox-'+key):
        old=w.load(path) if path.exists() else {}
        if old.get('github_login') and old['github_login'].lower()!=client.login.lower() and old.get('status') not in ('withdrawn','withdrawn_or_not_ready'):
            # One human with two stored accounts must not create two reviews/votes.
            old['requested_logins']=sorted(set(old.get('requested_logins',[old['github_login']])+[client.login]))
            if active:old.update(status='linked',job=active['id'])
            w.save(path,old);return old
        status=old.get('status','pending')
        if status in ('draft_hold','withdrawn','withdrawn_or_not_ready','auth_wait'):status='pending'
        self_authored=pr.get('user',{}).get('login','').lower() in [x.lower() for x in config['logins']]
        if self_authored:status='self_authored'
        elif pr.get('draft') is not False:status='draft_hold'
        elif active:status='linked'
        if not pr.get('head',{}).get('sha'):raise RuntimeError('GitHub PR has no source head')
        record={**old,'id':key,'source_kind':'github','url':url,'account':client.login,'self_id':str(client.identity['id']),
            'github_login':client.login,'github_user_id':client.identity['id'],'status':status,
            'created_at':old.get('created_at',w.utc()),'updated_at':w.utc(),'attempts':old.get('attempts',0),
            'head':pr['head']['sha'],'assignment':{'login':client.login,'user_id':client.identity['id'],'api_path':api_path(url),
                'direct_request':requested,'required_or_optional':'all direct requests; individual requirement is determined by repository policy'}}
        if watch:
            record['assignment'].update(repository_watch=watch['repository'],
                required_or_optional='All open PRs in this explicitly authorized repository; no individual request is required')
        if active:record['job']=active['id']
        w.save(path,record);return record


def inbox():
    return sorted([w.load(path) for path in (w.ROOT/'inbox').glob('github-*.json')],key=lambda r:(r.get('created_at',''),r['id']))


def poll():
    if not config_path().exists():return
    config=w.load(config_path())
    if not config.get('enabled'):return
    import configuration
    configuration.intake('github', config, require_enabled=True)
    with w.lock('github-intake',nonblocking=True) as acquired:
        if not acquired:return
        previous=w.load(state_path()) if state_path().exists() else {};accounts={}
        try:watches=repository_watches(config)
        except ValueError as error:
            w.save(state_path(),{**previous,'last_poll_at':w.utc(),'complete':False,'configuration_error':str(error)})
            if previous.get('configuration_error')!=str(error):w.notify('GitHub repository-watch configuration is invalid. Inspect github-inbox.')
            return
        for login in config.get('logins',[]):
            try:
                client=GitHubClient(login);observed={};direct_seen=set();items=[];direct_complete=False;repositories={};errors=[]
                try:
                    items=client.requested()
                    for item in items:
                        url=item.get('html_url')
                        if not url or 'pull_request' not in item:continue
                        pr=client.get(api_path(url));request=record(config,client,pr)
                        if request:
                            observed[request['id']]=request
                            if direct(pr,login):direct_seen.add(request['id'])
                    direct_complete=True
                except Exception as error:errors.append(type(error).__name__)
                for watch in (entry for entry in watches if entry['login'].lower()==login.lower()):
                    repository=watch['repository'];seen=set()
                    try:
                        prs=client.repository_prs(repository)
                        for pr in prs:
                            if repository_for_url(pr['html_url']).lower()!=repository.lower():
                                raise RuntimeError('Repository PR routing does not match the authorized source')
                            request=record(config,client,pr)
                            if request:observed[request['id']]=request;seen.add(request['id'])
                        repositories[repository]={'complete':True,'last_success_at':w.utc(),'open_results':len(prs),
                            'intake_results':len(seen),'without_direct_request':sum(not r['assignment']['direct_request'] for r in observed.values()
                                if r['id'] in seen)}
                    except Exception as error:
                        repositories[repository]={'complete':False,'error':type(error).__name__};errors.append(type(error).__name__)
                for old in inbox():
                    if old.get('github_login','').lower()!=login.lower() or old['id'] in observed or old['status'] not in ('pending','draft_hold'):continue
                    watch=watch_for(config,old['url'],login)
                    complete=repositories.get(watch['repository'],{}).get('complete',False) if watch else direct_complete
                    if complete:teams_intake.update_request(old['id'],{'status':'withdrawn','updated_at':w.utc()})
                counts=Counter(request['status'] for request in observed.values())
                complete=direct_complete and all(item.get('complete') for item in repositories.values())
                accounts[login]={'complete':complete,'user_id':client.identity['id'],'direct_request_scan_complete':direct_complete,
                    'search_results':len(items),'direct_results':len(direct_seen),'intake_results':len(observed),
                    'non_direct_or_changed_requests':max(0,len(items)-len(direct_seen)),
                    'repositories':repositories,'statuses':dict(counts)}
                if complete:accounts[login]['last_success_at']=w.utc()
                elif errors:accounts[login]['error']=errors[0]
            except Exception as error:accounts[login]={'complete':False,'error':type(error).__name__}
            error=accounts[login].get('error')
            if error and previous.get('accounts',{}).get(login,{}).get('error')!=error:
                w.notify('GitHub intake incomplete for '+login+' ('+error+'). Inspect github-inbox and stored-account sign-in.')
        state={'last_poll_at':w.utc(),'accounts':accounts,'complete':bool(accounts) and all(x.get('complete') for x in accounts.values()),
            'scope':'Direct individual requests across accessible repositories, plus all open PRs in explicitly configured repository watches'}
        if state['complete']:state['last_success_at']=w.utc()
        elif previous.get('last_success_at'):state['last_success_at']=previous['last_success_at']
        w.save(state_path(),state)


def verify_request(config,request):
    login=request.get('github_login')
    if not config.get('enabled') or login not in config.get('logins',[]):return False
    if request.get('assignment',{}).get('api_path')!=api_path(request['url']):return False
    client=GitHubClient(login)
    if client.identity['id']!=request.get('github_user_id'):return False
    pr=client.get(request['assignment']['api_path'])
    authorized=direct(pr,login) or bool(watch_for(config,request['url'],login))
    return (pr.get('state')=='open' and pr.get('draft') is False and authorized
        and pr.get('user',{}).get('login','').lower() not in [x.lower() for x in config['logins']]
        and w.canonical_url(pr['html_url']).lower()==request['url'].lower())


def enrolled_watch_authorized(job):
    if not job.get('github_repository_watch'):return True
    if not config_path().exists():return False
    try:
        config=w.load(config_path())
        watch=watch_for(config,job['url'],job.get('github_login',''))
        return bool(watch and watch['repository'].lower()==job['github_repository_watch'].lower())
    except (ValueError,TypeError):return False


def prepare_one(request_id=None):
    if not config_path().exists():return False
    config=w.load(config_path())
    if not config.get('enabled') or not config.get('auto_start_reviews') or len(w.recent_cycles())>=w.global_cycle_limit():return False
    for request in inbox():
        if request_id is not None and request['id']!=request_id:continue
        if request['status']=='preparing':
            pid=request.get('worker_pid')
            if pid and w.process_identity(pid)==request.get('worker_identity'):continue
            request=teams_intake.update_request(request['id'],{'status':'pending','updated_at':w.utc()})
        if request['status']!='pending' or request.get('next_retry_epoch',0)>time.time():continue
        existing=next((j for j in w.jobs() if j['status']!='done' and parallel_dispatch.pr_key(j['url'])==parallel_dispatch.pr_key(request['url'])),None)
        if existing:
            teams_intake.update_request(request['id'],{'status':'linked','job':existing['id']});continue
        try:
            if not verify_request(config,request):
                teams_intake.update_request(request['id'],{'status':'withdrawn_or_not_ready','updated_at':w.utc()});continue
        except github_auth.GitHubAuthError:
            teams_intake.update_request(request['id'],{'status':'auth_wait','github_auth_hold':True,'last_error':'GitHub sign-in needs help; the watcher will verify recovery'});continue
        except Exception as error:
            teams_intake.update_request(request['id'],{'last_error':'GitHub assignment verification unavailable ('+type(error).__name__+')','next_retry_epoch':time.time()+300});continue
        return teams_intake.prepare_request(request)
    return False


def coverage_summary():
    config=w.load(config_path()) if config_path().exists() else {}
    state=w.load(state_path()) if state_path().exists() else {}
    return {**state,'enabled':bool(config.get('enabled')),'auto_start_reviews':bool(config.get('auto_start_reviews')),
            'configured_logins':config.get('logins',[]),'configured_repository_watches':config.get('repository_watches',[]),
            'group_only_assignments':'not automatically enrolled; explicit repository watches do not depend on team requests',
            'reviewed_requests':'Existing enrolled jobs remain watched after GitHub clears the request on submitted review'}


if __name__=='__main__':
    import argparse,os
    os.umask(0o077);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['poll','inbox']);args=parser.parse_args()
    if args.command=='poll':poll()
    else:print(json.dumps({'requests':[{k:r.get(k) for k in ['id','url','status','job','github_login','last_error']} for r in inbox()],'coverage':coverage_summary()},indent=2))
