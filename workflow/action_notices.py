"""Brief local review notices. Provider reads verify completed actions."""
from datetime import datetime
import os
import re
import time
import unicodedata

import workflow as w


def config():
    path = w.ROOT / 'action-notices-config.json'
    return w.load(path) if path.exists() else {}


def enabled(subject):
    source = subject.get('intake_source', subject.get('source_kind'))
    if source is None:
        source = 'teams' if subject.get('id', '').startswith('teams-') else None
    if source is None:
        source = 'explicit'
    return (config().get('enabled') is True and source in config().get('sources', ['teams', 'ado'])
            and subject.get('mode', 'review') == 'review')


def identity(subject):
    url = w.canonical_url(subject['url'])
    if url.startswith('https://github.com/'):
        parts = url.split('/')
        return ['github', parts[3].lower(), parts[4].lower(), parts[-1]]
    parts = url.split('/')
    return ['ado', parts[3].lower(), parts[-1]]


def label(subject):
    parts = identity(subject)
    return parts[-1] if parts[0] == 'ado' else '/'.join(parts[1:3]) + '#' + parts[-1]


def state_path(subject):
    return w.ROOT / 'action-notices' / 'state' / (w.digest(identity(subject)) + '.json')


def actor_name(value):
    if not isinstance(value, str):
        return None
    value = ''.join(c for c in value if not unicodedata.category(c).startswith('C'))
    value = ' '.join(value.split()[:4])[:48].strip()
    return value or None


def message(subject, kind, actor=None, reason=None, reapproval=False):
    pr = label(subject)
    if kind == 'preparation':
        return 'Prepare review context for PR ' + pr
    if kind == 'first-review':
        return 'First review on PR ' + pr
    if kind == 'rereview':
        name = actor_name(actor)
        suffix = " for " + name + "'s fixes/comments" if name else ' after new commits or comments'
        return 'Rereview PR ' + pr + suffix
    if kind == 'validation':
        return 'Review CI results for PR ' + pr
    if kind == 'resume':
        action = {'comment': 'post a comment', 'approval': 'submit approval', 'publish': 'publish the PR'}.get(reason)
        return ('Resume PR ' + pr + ' to ' + action) if action else 'Resume review on PR ' + pr
    if kind == 'comment':
        return 'Posted a review comment on PR ' + pr
    if kind == 'comment-updated':
        return 'Updated a review comment on PR ' + pr
    if kind == 'resolved':
        return 'Review comment marked resolved on PR ' + pr
    if kind == 'approval':
        return ('Reapproved PR ' if reapproval else 'Approved PR ') + pr
    if kind == 'merged':
        return 'PR ' + pr + ' merged'
    if kind == 'closed':
        return 'PR ' + pr + ' closed without merge'
    if kind == 'blocked':
        return 'Review blocked on PR ' + pr
    raise ValueError('Unsupported review notice')


def emit(subject, kind, key, **fields):
    if not enabled(subject):
        return None
    event_id = w.digest([identity(subject), kind, key])
    path = w.ROOT / 'action-notices' / 'events' / (event_id + '.json')
    with w.lock('action-event-' + event_id):
        if path.exists():
            return {**w.load(path), 'new': False}
        record = {'id': event_id, 'at': w.utc(), 'order': time.time_ns(), 'job': subject['id'],
                  'url': w.canonical_url(subject['url']), 'kind': kind,
                  'text': message(subject, kind, **fields),
                  'desktop_requested': config().get('desktop', True) is True,
                  'notification_request_accepted': False}
        w.save(path, record)
    if record['desktop_requested']:
        record['notification_request_accepted'] = bool(w.notify(record['text']))
        w.save(path, record)
    return {**record, 'new': True}


def safe(function, *args, **kwargs):
    """Keep a notice failure separate from the PR result."""
    try:
        return function(*args, **kwargs)
    except Exception as error:
        try:
            w.save(w.ROOT / 'attention' / 'action-notices.json', {
                'at': w.utc(), 'state': 'notice_error', 'error': type(error).__name__,
                'message': 'A local action notice failed. PR ownership and validation rules remain unchanged.'})
        except OSError:
            pass
        return None


def self_identity(subject):
    if subject['url'].startswith('https://github.com/'):
        return str(subject.get('github_login') or subject.get('github_user_id') or '').lower()
    path = w.ROOT / 'auth' / 'ado.json'
    expected = w.load(path).get('expected_identity_id') if path.exists() else None
    request_id = subject.get('intake')
    request_path = w.ROOT / 'inbox' / (request_id + '.json') if request_id else None
    assigned = None
    if request_path and request_path.exists():
        request = w.load(request_path)
        if request.get('source_kind') == 'ado':
            assigned = request.get('self_id')
    if expected and assigned and expected.lower() != assigned.lower():
        return ''
    return str(expected or assigned or '').lower()


def author_identity(author):
    if isinstance(author, str):
        return author.lower()
    return str((author or {}).get('id') or (author or {}).get('login')
               or (author or {}).get('uniqueName') or '').lower()


def recent(value):
    since = config().get('since')
    if not since or not value:
        return True
    try:
        normalize = lambda s: datetime.fromisoformat(re.sub(r'\.(\d{6})\d+', r'.\1', s).replace('Z', '+00:00'))
        return normalize(value) >= normalize(since)
    except (ValueError, TypeError):
        return False


def compact(subject, data):
    own = self_identity(subject)
    github = subject['url'].startswith('https://github.com/')
    comments, threads = {}, {}

    def add(comment, thread=None):
        if comment.get('isDeleted') or comment.get('commentType') == 'system' or comment.get('id') is None:
            return
        author = comment.get('author') or {}
        key = str(comment['id']) if github else str(thread) + ':' + str(comment['id'])
        comments[key] = {'own': bool(own and author_identity(author) == own),
                         'actor': actor_name(author if isinstance(author, str) else author.get('displayName') or author.get('login')),
                         'digest': w.digest(comment.get('content', comment.get('body', ''))),
                         'created': comment.get('publishedDate') or comment.get('created_at'),
                         'updated': comment.get('lastContentUpdatedDate') or comment.get('lastUpdatedDate') or comment.get('updated_at')}

    for thread in data.get('threads', []):
        raw = thread.get('comments') or []
        if isinstance(raw, dict):
            raw = raw.get('nodes', [])
        for comment in raw:
            add(comment, thread.get('id'))
        first = next((c for c in raw if c.get('commentType') != 'system'), {})
        threads[str(thread.get('id'))] = {
            'resolved': thread.get('status') == 'fixed' or thread.get('isResolved') is True,
            'own': bool(own and author_identity(first.get('author')) == own)}
    for key in ('comments', 'inline_comments'):
        for comment in data.get(key, []):
            add(comment)
    vote, approval_id = None, None
    if github:
        reviews = [r for r in data.get('reviews', []) if own and author_identity(r.get('author')) == own
                   and r.get('state') in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED')]
        if reviews:
            latest = max(reviews, key=lambda r: r.get('submitted_at') or '')
            vote = 10 if latest.get('state') == 'APPROVED' and latest.get('commit_id') == data.get('head') else 0
            approval_id = str(latest.get('id')) if vote == 10 else None
    else:
        vote = next((r.get('vote') for r in data.get('reviewers', []) if own and str(r.get('id', '')).lower() == own), None)
    return {'head': data.get('head'), 'state': data.get('state'), 'comments': comments, 'threads': threads,
            'vote': vote, 'approval_id': approval_id,
            'self_authored': bool(own and author_identity(data.get('author')) == own),
            'validation': w.digest({k: data.get(k) for k in ('policies', 'checks', 'statuses', 'extra_builds')})}


def seed(subject, data):
    if not enabled(subject) or not data.get('head') or not data.get('state'):
        return
    path = state_path(subject)
    with w.lock('action-state-' + path.stem):
        if path.exists():
            return
        current = compact(subject, data)
        w.save(path, {'snapshot': current, 'approval_sequence': 0,
                      'approved_before': current['vote'] == 10, 'trigger': {}, 'pending': []})


def approval_key(state, current):
    return [current['head'], current.get('approval_id') or state.get('approval_sequence', 0)]


def observe(subject, data):
    if not enabled(subject) or not data.get('head') or not data.get('state'):
        return False
    path = state_path(subject)
    with w.lock('action-state-' + path.stem):
        state = w.load(path) if path.exists() else {'approval_sequence': 0, 'approved_before': False, 'pending': []}
        current = compact(subject, data)
        previous = state.get('snapshot')
        trigger = {}
        if previous:
            changed = [(key, c) for key, c in current['comments'].items()
                       if previous['comments'].get(key, {}).get('digest') != c['digest']]
            actors = [c['actor'] for _, c in changed if not c['own'] and c['actor']]
            if current['head'] != previous['head'] or actors or current['threads'] != previous['threads']:
                trigger = {'kind': 'rereview', 'actor': actors[-1] if actors else None}
            elif current['validation'] != previous['validation']:
                trigger = {'kind': 'validation'}
            if previous['head'] != current['head'] or (previous['vote'] == 10 and current['vote'] != 10):
                state['approval_sequence'] = state.get('approval_sequence', 0) + 1
            for key, comment in changed:
                if comment['own'] and recent(comment.get('updated') or comment.get('created')):
                    kind = 'comment-updated' if key in previous['comments'] else 'comment'
                    event_key = [key, comment['digest']] if kind == 'comment-updated' else key
                    emit(subject, kind, event_key)
            for key, thread in current['threads'].items():
                before = previous['threads'].get(key)
                if before and thread['own'] and thread['resolved'] and not before['resolved']:
                    emit(subject, 'resolved', [key, current['head']])
            fresh_approval = (previous['vote'] != 10 or (current.get('approval_id')
                              and current['approval_id'] != previous.get('approval_id')))
            if current['vote'] == 10 and fresh_approval and not current['self_authored']:
                emit(subject, 'approval', approval_key(state, current), reapproval=state.get('approved_before', False))
                state['approved_before'] = True
            if previous['state'] not in w.TERMINAL and current['state'] in w.TERMINAL:
                emit(subject, 'merged' if current['state'] in ('completed', 'merged') else 'closed', current['state'])
        else:
            state['approved_before'] = current['vote'] == 10
        for pending in state.get('pending', []):
            if verified(pending, current):
                if pending['kind'] == 'approval':
                    emit(subject, 'approval', approval_key(state, current), reapproval=state.get('approved_before', False))
                    state['approved_before'] = True
                else:
                    emit(subject, 'comment', pending['comment_key'])
        state['pending'] = [p for p in state.get('pending', []) if not verified(p, current)]
        state.update(snapshot=current, trigger=trigger or state.get('trigger', {}), observed_at=w.utc())
        w.save(path, state)
    return True


def preparation_started(request):
    return emit(request, 'preparation', request['id'])


def review_started(subject, data):
    if not enabled(subject):
        return None
    if not isinstance(data, dict):
        data = w.load(data)
    seed(subject, data)
    state = w.load(state_path(subject))
    trigger = state.get('trigger', {})
    pending = subject.get('pending_action')
    initial = not subject.get('last_worker_log') and not subject.get('last_checkpoint_cycle')
    kind = 'first-review' if initial else ('resume' if pending else trigger.get('kind', 'resume'))
    key = subject.get('attention_epoch') or w.fingerprint(data)
    return emit(subject, kind, [key, pending], actor=trigger.get('actor'), reason=pending)


def verified(request, current):
    if request['head'] != current['head']:
        return False
    if request['kind'] == 'approval':
        return current['vote'] == 10 and not current['self_authored']
    comment = current['comments'].get(request['comment_key'])
    return bool(comment and comment['own'] and recent(comment.get('created')))


def report(args):
    subject = w.load(w.job_path(args.id))
    worker = os.environ.get('AUTOWORKFLOW_JOB')
    session = os.environ.get('PI_SESSION_ID') or os.environ.get('CLAUDE_SESSION_ID')
    if os.environ.get('AUTOWORKFLOW_INTAKE') or (worker and worker != args.id):
        raise ValueError('A scoped worker can report only its own review actions')
    if not worker and (not session or session != subject.get('session') or not subject.get('owner')):
        raise ValueError('Only the owning review session can report this action')
    if subject['status'] in ('paused', 'done', 'blocked') or not enabled(subject):
        raise ValueError('Review action notices are not active for this job')
    if not re.fullmatch(r'[0-9a-f]{40}', args.head):
        raise ValueError('A review action requires the exact source SHA')
    if args.kind == 'comment' and (not args.comment_id or (not args.thread_id and not subject['url'].startswith('https://github.com/'))):
        raise ValueError('A comment notice requires the posted comment and thread IDs')
    request = {'kind': args.kind, 'head': args.head}
    if args.kind == 'comment':
        request['comment_key'] = str(args.comment_id) if subject['url'].startswith('https://github.com/') else str(args.thread_id) + ':' + str(args.comment_id)
    saved = w.ROOT / 'snapshots' / (subject['id'] + '.json')
    if saved.exists():
        seed(subject, w.load(saved))
    try:
        data = w.snapshot({**subject, 'include_terminal_threads': args.kind == 'comment'})
    except (Exception, SystemExit):
        path = state_path(subject)
        with w.lock('action-state-' + path.stem):
            state = w.load(path) if path.exists() else {}
            pending = state.setdefault('pending', [])
            if request not in pending:
                pending.append(request)
            w.save(path, state)
        return {'state': 'awaiting_verification', 'message': 'No completed-action notice sent. The existing provider probe will retry verification.'}
    current = compact(subject, data)
    if not verified(request, current):
        raise ValueError('Live provider evidence does not verify this completed review action')
    observe(subject, data)
    path = state_path(subject)
    with w.lock('action-state-' + path.stem):
        state = w.load(path)
        if args.kind == 'approval':
            result = emit(subject, 'approval', approval_key(state, current), reapproval=state.get('approved_before', False))
            state['approved_before'] = True
        else:
            result = emit(subject, 'comment', request['comment_key'])
        w.save(path, state)
    return result


def history(limit=20):
    if not 1 <= limit <= 200:
        raise ValueError('Action history limit must be between 1 and 200')
    records = []
    for path in (w.ROOT / 'action-notices' / 'events').glob('*.json'):
        try:
            records.append(w.load(path))
        except (OSError, ValueError):
            continue
    return sorted(records, key=lambda r: r['order'], reverse=True)[:limit]
