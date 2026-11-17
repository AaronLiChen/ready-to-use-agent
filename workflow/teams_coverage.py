"""Cursor-based direct coverage of every accessible joined channel and known chat.

Read-only Graph operations through WorkIQ. Unfinished pages never advance watermarks.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import copy
from collections import deque
import threading
import time
import urllib.parse

import workflow as w
import teams_intake as t

_READ_GATE=threading.Lock()
_LAST_READ=0.0
_LOCAL=threading.local()


def throttle(weight=1):
    global _LAST_READ
    with _READ_GATE:
        wait=_LAST_READ-time.monotonic()
        if wait>0:time.sleep(wait)
        _LAST_READ=time.monotonic()+.3*weight


def fetch(config,path):
    prefetched=getattr(_LOCAL,'prefetched',{})
    if path in prefetched:return prefetched.pop(path)
    throttle()
    return t.fetch(config,path)


def first_path(config,source,previous):
    if previous.get('reply_cursors'):return previous['reply_cursors'][0]['next']
    if previous.get('next'):return previous['next']
    base=base_path(source)
    if source['kind']=='channel':return base+'?$top=50&$expand=replies'
    lower=t.instant(config['since'])
    if previous.get('last_success_at'):lower=max(lower,t.instant(previous['last_success_at'])-timedelta(minutes=10))
    return base+'?'+urllib.parse.urlencode({'$top':'50','$orderby':'lastModifiedDateTime desc',
        '$filter':'lastModifiedDateTime gt '+lower.isoformat().replace('+00:00','Z')})


def discover_chats(config,state,deadline):
    cached_path=w.ROOT/'teams-chat-catalogue.json'
    if cached_path.exists():
        cached=w.load(cached_path)
        if cached.get('last_saved_epoch',0)>state.get('chat_catalogue',{}).get('last_saved_epoch',0):state['chat_catalogue']=cached
    catalog=state.setdefault('chat_catalogue',{'items':{}})
    if not catalog.get('next') and time.time()-catalog.get('last_complete_epoch',0)<3600:return
    path=catalog.get('next') or '/me/chats?$top=50&$select=id,chatType,topic'
    for _ in range(10):
        if time.monotonic()>=deadline:break
        data=fetch(config,path)
        if not isinstance(data.get('value'),list):raise t.ReadError('Chat catalogue missing; coverage incomplete')
        for chat in data['value']:
            if not chat.get('id'):continue
            catalog.setdefault('items',{})[chat['id']]={'kind':'chat','chat_id':chat['id'],'chat_type':chat.get('chatType'),'name':chat.get('topic')}
        path=data.get('@odata.nextLink')
        if not path:
            catalog.update(next=None,last_complete_epoch=time.time(),last_complete_at=w.utc());return
        path=t.graph_path(path);catalog['next']=path
    catalog['next']=path


def base_path(source):
    quote=lambda value:urllib.parse.quote(value,safe='')
    if source['kind']=='chat':return '/chats/'+quote(source['chat_id'])+'/messages'
    return '/teams/'+quote(source['team_id'])+'/channels/'+quote(source['channel_id'])+'/messages'


def message_time(message):
    return max(t.instant(message.get('createdDateTime')),t.instant(message.get('lastEditedDateTime')),t.instant(message.get('lastModifiedDateTime')))


def record_message(config,message,source,base,root=None,parent_context=None):
    path=(root+'/replies/' if root else base+'/')+urllib.parse.quote(str(message.get('id','')),safe='')
    t.record(config,message,{**source,'message_base':base,'graph_path':path,'web_url':message.get('webUrl'),'discovery':'all_source_direct'},
             context_sources=parent_context,resolve_parent=False)


def scan_source(config,source,previous,deadline,started_at=None):
    state=copy.deepcopy(previous);base=base_path(source);started=started_at or w.utc()
    state.update(name=source.get('name'),kind=source['kind'],last_attempt_at=started,last_attempt_epoch=time.time(),error=None)
    if not state.get('cycle_started_at'):
        lower=t.instant(config['since'])
        if state.get('last_success_at'):lower=max(lower,t.instant(state['last_success_at'])-timedelta(minutes=10))
        state.update(cycle_started_at=started,lower=lower.isoformat(),reply_cursors=[],root_done=False)
        if source['kind']=='chat':
            query=urllib.parse.urlencode({'$top':'50','$orderby':'lastModifiedDateTime desc',
                '$filter':'lastModifiedDateTime gt '+lower.isoformat().replace('+00:00','Z')})
            state['next']=base+'?'+query
        else:state['next']=base+'?$top=50&$expand=replies'
    pages=0
    try:
        while pages<3 and time.monotonic()<deadline:
            replies=state.get('reply_cursors',[])
            if replies:
                cursor=replies[0];data=fetch(config,cursor['next']);pages+=1
                values=data.get('value')
                if not isinstance(values,list):raise t.ReadError('Missing reply page; coverage incomplete')
                for message in values:record_message(config,message,source,base,cursor['root'],cursor.get('parent_context'))
                next_path=data.get('@odata.nextLink')
                if next_path:
                    next_path=t.graph_path(next_path)
                    if next_path==cursor['next']:raise t.ReadError('Reply pagination did not advance')
                    cursor['next']=next_path
                else:replies.pop(0)
                continue
            if state.get('root_done') or not state.get('next'):break
            current=state['next'];data=fetch(config,current);pages+=1
            values=data.get('value')
            if not isinstance(values,list):raise t.ReadError('Missing source page; coverage incomplete')
            last_activity=None
            for message in values:
                record_message(config,message,source,base)
                activity=message_time(message)
                root=base+'/'+urllib.parse.quote(str(message.get('id','')),safe='')
                parent_urls=t.linked_prs(config,message)
                parent_refs=[{'graph_path':root,'url':parent_urls[0]}] if len(parent_urls)==1 else None
                for reply in message.get('replies',[]):
                    record_message(config,reply,source,base,root,parent_refs)
                    activity=max(activity,message_time(reply))
                next_reply=message.get('replies@odata.nextLink')
                if next_reply:state.setdefault('reply_cursors',[]).append({'root':root,'next':t.graph_path(next_reply),'parent_context':parent_refs})
                last_activity=activity
            next_path=data.get('@odata.nextLink')
            if next_path:
                next_path=t.graph_path(next_path)
                if next_path==current:raise t.ReadError('Source pagination did not advance')
            # Graph documents channel ordering by the latest modification of the
            # entire reply chain. Do not use this boundary while replies remain.
            boundary=(source['kind']=='channel' and last_activity is not None and
                      last_activity<t.instant(state['lower']) and not state.get('reply_cursors'))
            state['next']=None if boundary else next_path
            state['root_done']=not bool(state['next'])
        if state.get('root_done') and not state.get('reply_cursors'):
            state.update(last_success_at=state['cycle_started_at'],last_success_epoch=time.time(),complete=True)
            for key in ['cycle_started_at','lower','next','root_done','reply_cursors']:state.pop(key,None)
        else:state['complete']=False
    except Exception as error:
        state.update(error=str(error) if isinstance(error,t.ReadError) else 'Source read unavailable ('+type(error).__name__+')',complete=False)
    state['last_pages_read']=pages
    return state


def poll_sources(config,state,deadline):
    try:
        discover_chats(config,state,min(deadline,time.monotonic()+35))
        state.pop('chat_catalogue_error',None)
    except t.ReadError as error:state['chat_catalogue_error']=str(error)
    sources=list(state.get('channels',{}).values())+list(state.get('chat_catalogue',{}).get('items',{}).values())
    saved=state.setdefault('source_coverage',{})
    for path in (w.ROOT/'teams-source-coverage').glob('*.json'):
        entry=w.load(path);source_id=entry.pop('source_id')
        if entry.get('last_attempt_epoch',0)>saved.get(source_id,{}).get('last_attempt_epoch',0):saved[source_id]=entry
    state.setdefault('chat_catalogue',{})['last_saved_epoch']=time.time()
    w.save(w.ROOT/'teams-chat-catalogue.json',state['chat_catalogue'])
    key=lambda source:source.get('chat_id') or source['channel_id']
    # Reserve regular turns for unfinished cursors/errors while discovering new
    # sources. A growing catalogue must not starve an already-partial thread.
    sources.sort(key=lambda source:saved.get(key(source),{}).get('last_attempt_epoch',0))
    due=[source for source in sources if time.time()-saved.get(key(source),{}).get('last_attempt_epoch',0)>=60]
    pinned=[source for source in due if source.get('channel_id') in config.get('direct_channel_ids',[])]
    remainder=[source for source in due if source not in pinned]
    unseen=deque(source for source in remainder if key(source) not in saved)
    partial=deque(source for source in remainder if key(source) in saved and not saved[key(source)].get('complete'))
    completed=deque(source for source in remainder if key(source) in saved and saved[key(source)].get('complete'))
    ordered=list(pinned)
    while unseen or partial or completed:
        for _ in range(2):
            if unseen:ordered.append(unseen.popleft())
        if partial:ordered.append(partial.popleft())
        if completed:ordered.append(completed.popleft())
    queue=deque(ordered);queue_lock=threading.Lock();updates={};stop=threading.Event()
    def worker():
        while not stop.is_set() and time.monotonic()<deadline:
            if config.get('background_silent_only'):
                import teams_reader
                if teams_reader.backoff_active():return
            with queue_lock:
                if not queue:return
                batch=[queue.popleft() for _ in range(min(4,len(queue)))]
            started=w.utc()
            paths=[first_path(config,source,saved.get(key(source),{})) for source in batch]
            _LOCAL.prefetched={}
            if len(batch)>1:
                try:
                    throttle(len(batch));values=t.fetch_many(config,paths)
                    _LOCAL.prefetched=dict(zip(paths,values))
                except t.ReadError:
                    if config.get('background_silent_only') and teams_reader.backoff_active():return
            for source in batch:
                if stop.is_set() or time.monotonic()>=deadline:break
                result=scan_source(config,source,saved.get(key(source),{}),deadline,started_at=started)
                w.save(w.ROOT/'teams-source-coverage'/(w.digest(key(source))+'.json'),{'source_id':key(source),**result})
                with queue_lock:updates[key(source)]=result
            _LOCAL.prefetched={}
    try:
        parallelism=2 if config.get('background_silent_only') else 8
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            futures=[pool.submit(worker) for _ in range(parallelism)]
            for future in futures:future.result()
    finally:stop.set()
    saved.update(updates)
    for source in sources:
        entry=saved.get(key(source),{})
        if entry.get('error'):state.setdefault('coverage_gaps',{})[key(source)]=entry['error']
        elif entry.get('complete'):state.setdefault('coverage_gaps',{}).pop(key(source),None)
    complete=sum(bool(saved.get(key(source),{}).get('complete')) for source in sources)
    failed=sum(bool(saved.get(key(source),{}).get('error')) for source in sources)
    unfinished=sum(not bool(saved.get(key(source),{}).get('complete')) for source in sources)
    catalogue=state.get('chat_catalogue',{})
    catalogue_complete=bool(catalogue.get('last_complete_at')) and not bool(catalogue.get('next')) and not state.get('chat_catalogue_error')
    def fresh(source):
        stamp=saved.get(key(source),{}).get('last_success_at')
        return bool(stamp and time.time()-t.instant(stamp).timestamp()<=900 and saved.get(key(source),{}).get('complete'))
    fresh_count=sum(fresh(source) for source in sources)
    state['all_source_coverage']={'known_channels':len(state.get('channels',{})),
        'known_chats':len(state.get('chat_catalogue',{}).get('items',{})),'sources_attempted_this_poll':len(updates),
        'sources_with_completed_scan':complete,'unfinished_sources':unfinished,'failed_sources':failed,
        'chat_catalogue_complete':catalogue_complete,'fresh_sources':fresh_count,'stale_sources':len(sources)-fresh_count,
        'freshness_target_seconds':900,'complete':catalogue_complete and fresh_count==len(sources) and failed==0 and not state.get('discovery_gaps'),
        'oldest_success_at':min((saved.get(key(source),{}).get('last_success_at') for source in sources if saved.get(key(source),{}).get('last_success_at')),default=None),
        'note':'All accessible discovered sources are scheduled. Message/reply cursors persist; inaccessible or unfinished sources are not called complete.'}


def progress_summary(state):
    catalogue=w.load(w.ROOT/'teams-chat-catalogue.json') if (w.ROOT/'teams-chat-catalogue.json').exists() else state.get('chat_catalogue',{})
    known=set(state.get('channels',{}))|set(catalogue.get('items',{}))
    rows=[w.load(path) for path in (w.ROOT/'teams-source-coverage').glob('*.json')]
    rows=[row for row in rows if row.get('source_id') in known]
    fresh=[row for row in rows if row.get('complete') and row.get('last_success_at') and time.time()-t.instant(row['last_success_at']).timestamp()<=900]
    errors=[row for row in rows if row.get('error')]
    return {'known_channels':len(state.get('channels',{})),'known_chats':len(catalogue.get('items',{})),
        'chat_catalogue_complete':bool(catalogue.get('last_complete_at')) and not catalogue.get('next'),
        'sources_attempted':len(rows),'fresh_completed_sources':len(fresh),'unscanned_or_stale_sources':len(known)-len(fresh),
        'failed_sources':len(errors),'partial_sources':sum(not row.get('complete') for row in rows),
        'errors':[{'id':row['source_id'],'name':row.get('name'),'error':row['error']} for row in errors[:20]],
        'complete':bool(known) and len(fresh)==len(known) and not errors and bool(catalogue.get('last_complete_at')) and not catalogue.get('next') and not state.get('chat_catalogue_error') and not state.get('discovery_gaps')}


def run(config,state,deadline):
    t.discover(config,state)
    # Keep a time slice for source verification, held-context repair and indexed
    # reconciliation. Direct reads no longer depend on channel naming/search.
    poll_sources(config,state,min(deadline-25,time.monotonic()+180))
    if time.monotonic()<deadline:t.resolve_missing_context(config,deadline)
    if time.monotonic()<deadline-5:
        try:t.reconcile_search(config,state,timeout=max(5,min(25,int(deadline-time.monotonic()))));state.pop('search_error',None)
        except t.ReadError as error:state['search_error']=str(error)
