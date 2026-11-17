#!/usr/bin/env python3
"""Read-only organization-wide ADO reviewer intake through Azure CLI authentication."""
from collections import Counter
import json
from pathlib import Path
import re
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

import workflow as w
import probes
import teams_intake


def config_path():return w.ROOT/'ado-config.json'
def state_path():return w.ROOT/'ado-state.json'


class ADOClient:
    def __init__(self, organization):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,62}',organization):
            raise ValueError('Invalid configured ADO organization')
        self.organization=organization
        self.origin='https://dev.azure.com/'+organization+'/'
        import ado
        try:self.token=probes.acquire_ado_token(ado)
        except SystemExit as error:raise RuntimeError('ADO authentication unavailable; run az login') from error
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        self.opener=urllib.request.build_opener(NoRedirect())

    def get(self,path):
        if not path or path.startswith(('/', 'http:','https:')) or '..' in path.split('/'):
            raise ValueError('Refusing an unscoped ADO intake path')
        req=urllib.request.Request(self.origin+path,headers={'Authorization':'Bearer '+self.token,'Accept':'application/json'})
        try:
            with self.opener.open(req,timeout=40) as response:
                data=json.load(response);continuation=response.headers.get('x-ms-continuationtoken')
        except urllib.error.HTTPError as error:
            import auth_recovery
            auth_recovery.http_failure(error,resource_url=self.origin+path)
        if not isinstance(data,dict) or data.get('typeKey') or data.get('error'):
            raise RuntimeError('ADO returned an error or invalid response')
        return data,continuation

    def identity(self,account):
        data,_=self.get('_apis/connectionData?api-version=7.1-preview.1')
        user=data.get('authenticatedUser') or {}
        actual=((user.get('properties') or {}).get('Account') or {}).get('$value','')
        if actual.lower()!=account.lower() or not user.get('id'):
            import auth_recovery
            auth_recovery.failure('identity_mismatch')
            raise auth_recovery.AuthError('identity_mismatch')
        return {'id':user['id'],'account':actual,'display_name':user.get('providerDisplayName')}

    def assigned(self,reviewer):
        result=[];seen=set();skip=0;continuation=None
        for _ in range(1000):
            query={'searchCriteria.reviewerId':reviewer,'searchCriteria.status':'active','$top':100,'api-version':'7.1'}
            if continuation:query['continuationToken']=continuation
            elif skip:query['$skip']=skip
            data,next_token=self.get('_apis/git/pullrequests?'+urllib.parse.urlencode(query))
            page=data.get('value')
            if not isinstance(page,list):raise RuntimeError('Missing assigned-PR collection; not proof of no assignments')
            signature=probes.digest([p.get('pullRequestId') for p in page])
            if page and signature in seen:raise RuntimeError('ADO reviewer pagination did not advance')
            seen.add(signature);result.extend(page)
            if not next_token and len(page)<100:return result
            if next_token and next_token==continuation:raise RuntimeError('ADO continuation did not advance')
            continuation=next_token;skip+=len(page)
        raise RuntimeError('ADO reviewer pagination exceeded its bound; coverage incomplete')


def aliases(organization,pr):
    repo=pr.get('repository') or {};project=repo.get('project') or {}
    number=pr.get('pullRequestId')
    if not isinstance(number,int) or number<=0 or not all([repo.get('id'),repo.get('name'),project.get('id'),project.get('name')]):
        raise RuntimeError('Incomplete ADO PR routing metadata')
    quote=lambda s:urllib.parse.quote(str(s),safe='')
    return list(dict.fromkeys('https://dev.azure.com/'+organization+'/'+quote(p)+'/_git/'+quote(r)+'/pullrequest/'+str(number)
        for p in [project['name'],project['id']] for r in [repo['name'],repo['id']]))


def same_request(job,urls):
    return w.canonical_url(job['url']) in urls


def record(config,organization,identity,pr):
    reviewer=next((x for x in pr.get('reviewers',[]) if str(x.get('id','')).lower()==identity['id'].lower()),None)
    if not reviewer or pr.get('status')!='active':return None
    urls=aliases(organization,pr);url=urls[0]
    head=(pr.get('lastMergeSourceCommit') or {}).get('commitId')
    if not head:raise RuntimeError('Assigned PR has no source head')
    history=[j for j in w.jobs() if same_request(j,urls)]
    active=next((j for j in history if j['status']!='done'),None)
    generation=sum(j['status']=='done' for j in history)
    key='ado-'+w.digest(url)[:20]+('-r'+str(generation) if generation and not active else '')
    if active and str(active.get('intake','')).startswith('ado-'):key=active['intake']
    path=w.ROOT/'inbox'/(key+'.json')
    with w.lock('inbox-'+key):
        old=w.load(path) if path.exists() else {}
        status=old.get('status','pending')
        if status in ('draft_hold','withdrawn','withdrawn_or_not_ready','terminal'):status='pending'
        author=pr.get('createdBy') or {}
        self_authored=(str(author.get('id','')).lower()==identity['id'].lower() or
                       str(author.get('uniqueName','')).lower()==config['account'].lower())
        if self_authored:status='self_authored'
        elif pr.get('isDraft') is not False:status='draft_hold'
        elif active:status='linked'
        elif any(w.legacy_watcher(value) for value in urls):status='existing_watch'
        repo=pr['repository'];project=repo['project']
        entry={**old,'id':key,'source_kind':'ado','url':url,'url_aliases':urls,'account':config['account'],
            'self_id':identity['id'],'status':status,'created_at':old.get('created_at',w.utc()),'updated_at':w.utc(),
            'attempts':old.get('attempts',0),'head':head,'vote':reviewer.get('vote'),
            'required':bool(reviewer.get('isRequired')),'assignment':{'organization':organization,'project_id':project['id'],
                'project_name':project['name'],'repository_id':repo['id'],'repository_name':repo['name'],
                'pr_id':pr['pullRequestId'],'reviewer_id':identity['id'],
                'api_path':urllib.parse.quote(project['id'],safe='')+'/_apis/git/repositories/'+urllib.parse.quote(repo['id'],safe='')+
                    '/pullRequests/'+str(pr['pullRequestId'])+'?api-version=7.1'}}
        if active:entry['job']=active['id']
        w.save(path,entry)
        return entry


def inbox():
    return sorted([w.load(p) for p in (w.ROOT/'inbox').glob('ado-*.json')],key=lambda r:(r.get('created_at',''),r['id']))


def poll():
    if not config_path().exists():return
    config=w.load(config_path())
    if not config.get('enabled'):return
    import configuration
    configuration.intake('ado', config, require_enabled=True)
    with w.lock('ado-intake',nonblocking=True) as acquired:
        if not acquired:return
        state=w.load(state_path()) if state_path().exists() else {}
        organizations={}
        for organization in config.get('organizations',[]):
            try:
                client=ADOClient(organization);identity=client.identity(config['account'])
                prs=client.assigned(identity['id']);seen=set();counts=Counter();repositories=set();projects=set()
                for pr in prs:
                    entry=record(config,organization,identity,pr)
                    if not entry:counts['group_only_or_not_direct']+=1;continue
                    seen.add(entry['id']);counts[entry['status']]+=1
                    repositories.add(entry['assignment']['repository_id']);projects.add(entry['assignment']['project_id'])
                for prior in inbox():
                    if prior.get('assignment',{}).get('organization')!=organization or prior['id'] in seen:continue
                    if prior['status'] in ('pending','draft_hold'):
                        teams_intake.update_request(prior['id'],{'status':'withdrawn','updated_at':w.utc()})
                    elif prior['status']=='blocked':
                        try:
                            current,_=client.get(prior['assignment']['api_path'])
                            if current.get('status') in w.TERMINAL:
                                teams_intake.update_request(prior['id'],{'status':'terminal','terminal_state':current['status'],
                                    'terminal_observed_at':w.utc(),'decision':'PR ended before failed preparation recovered; no completed review is assumed.'})
                            elif current.get('status')=='active' and not any(str(r.get('id','')).lower()==identity['id'].lower() for r in current.get('reviewers',[])):
                                teams_intake.update_request(prior['id'],{'status':'withdrawn','updated_at':w.utc()})
                        except Exception as error:
                            teams_intake.update_request(prior['id'],{'last_error':'Terminal-state reconciliation unavailable ('+type(error).__name__+')'})
                organizations[organization]={'last_success_at':w.utc(),'complete':True,'identity':identity,
                    'active_results':len(prs),'direct_results':len(seen),'repositories':len(repositories),'projects_with_assignments':len(projects),'statuses':dict(counts)}
            except Exception as error:
                code=getattr(error,'code',None)
                organizations[organization]={'complete':False,'error':type(error).__name__+(' HTTP '+str(code) if code else ''),
                    'error_location':[Path(frame.filename).name+':'+str(frame.lineno)+':'+frame.name for frame in traceback.extract_tb(error.__traceback__)[-5:]]}
        state.update(last_poll_at=w.utc(),organizations=organizations,
            scope='Organization-wide active PR query; all accessible repositories/projects; direct required and optional user assignments')
        if organizations and all(x.get('complete') for x in organizations.values()):state['last_success_at']=w.utc()
        w.save(state_path(),state)


def verify_request(config,request):
    assignment=request.get('assignment') or {};organization=assignment.get('organization')
    if organization not in config.get('organizations',[]) or request.get('account','').lower()!=config['account'].lower():return False
    client=ADOClient(organization);identity=client.identity(config['account'])
    if identity['id'].lower()!=str(request.get('self_id','')).lower():return False
    pr,_=client.get(assignment['api_path'])
    if pr.get('status')!='active' or pr.get('isDraft') is not False:return False
    if pr.get('pullRequestId')!=assignment['pr_id'] or (pr.get('repository') or {}).get('id')!=assignment['repository_id']:return False
    author=pr.get('createdBy') or {}
    if str(author.get('id','')).lower()==identity['id'].lower() or str(author.get('uniqueName','')).lower()==config['account'].lower():return False
    return request['url'] in aliases(organization,pr) and any(str(x.get('id','')).lower()==identity['id'].lower() for x in pr.get('reviewers',[]))


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
        active=next((j for j in w.jobs() if j['status']!='done' and same_request(j,request['url_aliases'])),None)
        if active:
            teams_intake.update_request(request['id'],{'status':'linked','job':active['id']});continue
        if any(w.legacy_watcher(url) for url in request['url_aliases']):
            teams_intake.update_request(request['id'],{'status':'existing_watch'});continue
        try:
            if not verify_request(config,request):
                teams_intake.update_request(request['id'],{'status':'withdrawn_or_not_ready','updated_at':w.utc()});continue
        except Exception as error:
            import auth_recovery
            if isinstance(error,auth_recovery.AuthError):
                auth_recovery.failure('deferred',{'kind':'intake','id':request['id'],'url':request['url']})
                teams_intake.update_request(request['id'],{'status':'auth_wait','auth_hold':True,'last_error':'Authentication held; see agent-workflow auth-status','next_retry_epoch':time.time()+60})
            else:
                teams_intake.update_request(request['id'],{'last_error':'ADO assignment verification unavailable ('+type(error).__name__+')','next_retry_epoch':time.time()+1800})
            continue
        return teams_intake.prepare_request(request)
    return False


def coverage_summary():
    config=w.load(config_path()) if config_path().exists() else {}
    state=w.load(state_path()) if state_path().exists() else {}
    return {**state,'enabled':bool(config.get('enabled')),'auto_start_reviews':bool(config.get('auto_start_reviews')),
            'configured_organizations':config.get('organizations',[]),'group_only_assignments':'not automatically enrolled',
            'other_organizations':'not scanned unless configured'}


if __name__=='__main__':
    import argparse,os
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['poll','inbox']);args=parser.parse_args()
    if args.command=='poll':poll()
    else:print(json.dumps({'requests':[{k:r.get(k) for k in ['id','url','status','job','last_error']} for r in inbox()],'coverage':coverage_summary()},indent=2))
