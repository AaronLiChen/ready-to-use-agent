"""Bounded recovery for explicit model-provider preparation transport failures."""
from pathlib import Path
import os
import time

import workflow as w


def preparation_transport_failure(request,log_path,exit_code):
    if exit_code==0:return None
    path=Path(log_path)
    if not path.is_file() or path.stat().st_size>4096:return None
    # Do not infer this from repository text, tool output, or arbitrary summaries.
    output=path.read_text(errors='replace').strip()
    known={'Connection error.','Request timed out.','Connection error.\nConnection error.'}
    if output not in known:return None
    retries=request.get('transport_retry_count',0)
    if retries<2:
        return {'status':'pending','decision':None,'last_error':'Model-provider transport failure; bounded preparation retry scheduled',
                'transport_retry_count':retries+1,'next_retry_epoch':time.time()+60*(retries+1)}
    decision='Model-provider transport failed after two retries. Restore provider connectivity; retry this intake through the controller.'
    w.save(w.ROOT/'attention'/('model-transport-'+request['id']+'.json'),{
        'state':'needs_provider_help','updated_at':w.utc(),'id':request['id'],'url':request.get('url'),
        'reason':'Model-provider connection failure; no verified review handoff',
        'steps':['Verify the current Pi model provider can connect.',
                 'Run agent-workflow retry-intake '+request['id']+' after connectivity recovers. No review or approval is assumed.']})
    w.notify(request['id']+': model-provider connection needs help after bounded retries. See the persistent attention record.')
    return {'status':'blocked','decision':decision,'last_error':'Model-provider transport retry limit reached'}


def retry_intake(name):
    w.job_path(name)
    if os.environ.get('AUTOWORKFLOW_JOB') or os.environ.get('AUTOWORKFLOW_INTAKE'):
        raise ValueError('Scoped workers cannot restart intake ownership')
    with w.lock('inbox-'+name,nonblocking=True) as acquired:
        if not acquired:raise ValueError('Intake is active; coordinate before retrying')
        path=w.ROOT/'inbox'/(name+'.json');request=w.load(path)
        if request.get('status')!='blocked':raise ValueError('Only a blocked, idle intake may be explicitly retried')
        pid=request.get('worker_pid')
        if pid and w.process_identity(pid)==request.get('worker_identity'):raise ValueError('A live preparation still owns this request')
        if any(job['status']!='done' and job['url']==request.get('url') for job in w.jobs()):raise ValueError('An enrolled watch already owns this PR')
        request.update(status='pending',decision=None,next_retry_epoch=0,explicit_retry_at=w.utc())
        w.save(path,request)
        return {'id':name,'status':'pending','attempt_history_preserved':True}
