"""Scoped ADO comments and approvals. Keep credentials and response bodies out of output."""
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

import ado
import auth_recovery
import write_guard


def read_file(path):
    with Path(path).open('rb') as stream:
        data = stream.read(1048577)
    if len(data) > 1048576:
        raise ValueError('The managed comment file exceeds the supported size')
    return data.decode('utf-8')


def send(url, token, method, payload):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    request = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), method=method,
                                     headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json',
                                              'Content-Type': 'application/json; charset=utf-8'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            auth_recovery.http_failure(error, resource_url=url)
        raise RuntimeError('ADO write failed. Inspect provider state before retrying.') from None
    except (OSError, ValueError):
        raise RuntimeError('ADO receipt is uncertain. Inspect provider state before retrying.') from None


def execute(args):
    job = write_guard.assigned()
    org, project, repo, number = ado.parse_pr_input(job['url'])
    kind = 'approval' if args.approve else 'comment'
    write_guard.authorize(kind, args.head, job)
    if args.approve and (args.reply_thread is not None or args.thread_context):
        raise ValueError('Approval does not accept comment thread options')
    if args.reply_thread is not None and (args.reply_thread <= 0 or args.thread_context):
        raise ValueError('Use one positive reply thread or a new inline context')
    payload = None
    if not args.approve:
        text = read_file(args.comment_file)
        if not text.strip():
            raise ValueError('An ADO review comment must contain text')
        comment = {'parentCommentId': 0, 'content': text, 'commentType': 1}
        payload = comment if args.reply_thread is not None else {'comments': [comment], 'status': 1}
        if args.thread_context:
            context = json.loads(read_file(args.thread_context))
            allowed = {'filePath', 'leftFileStart', 'leftFileEnd', 'rightFileStart', 'rightFileEnd'}
            if not isinstance(context, dict) or not set(context).issubset(allowed) or not context.get('filePath'):
                raise ValueError('Use an explicit ADO file and line context')
            payload['threadContext'] = context
    token = auth_recovery.acquire(ado, force=True)
    identity = ado._get(ado.origin(org) + '_apis/connectionData?api-version=7.1-preview.1', token)
    user = identity.get('authenticatedUser') or {}
    account = ((user.get('properties') or {}).get('Account') or {}).get('$value') or ''
    if not user.get('id') or account.lower() != auth_recovery.expected_account().lower():
        auth_recovery.failure('identity_mismatch')
        raise auth_recovery.AuthError('identity_mismatch')
    current = ado.api_get_pr(org, project, repo, number, token)
    if (current.get('status') != 'active' or (current.get('lastMergeSourceCommit') or {}).get('commitId') != args.head
            or (args.approve and current.get('isDraft') is not False)
            or (job.get('expected_target') and current.get('targetRefName') != job['expected_target'])):
        raise ValueError('ADO source, target, draft, or state changed before the write')
    base = (ado.api_base(org, project) + 'git/repositories/' + urllib.parse.quote(repo, safe='')
            + '/pullRequests/' + str(number))
    if args.approve:
        suffix = '/reviewers/' + urllib.parse.quote(user['id'], safe='')
        result = send(base + suffix + '?api-version=7.1', token, 'PUT', {'vote': 10})
        if result.get('id') != user['id'] or result.get('vote') != 10:
            raise RuntimeError('ADO approval receipt is uncertain. Inspect the live vote before retrying.')
        return {'head': args.head, 'reviewer_id': user['id'], 'vote': 10}
    suffix = '/threads' + ('/' + str(args.reply_thread) + '/comments' if args.reply_thread is not None else '')
    result = send(base + suffix + '?api-version=7.1', token, 'POST', payload)
    if not result.get('id'):
        raise RuntimeError('ADO comment receipt is uncertain. Inspect the live thread before retrying.')
    if args.reply_thread is not None:
        return {'head': args.head, 'thread_id': args.reply_thread, 'comment_id': result['id']}
    comments = result.get('comments') or []
    if not comments or not comments[0].get('id'):
        raise RuntimeError('ADO comment receipt is uncertain. Inspect the live thread before retrying.')
    return {'head': args.head, 'thread_id': result['id'], 'comment_id': comments[0]['id']}
