#!/usr/bin/env python3
"""Optional read-only, silent Teams reader. The operator supplies the implementation."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.parse

import workflow as w
import configuration


def state_path(): return w.ROOT / 'auth/teams-reader.json'
def action_path(): return w.ROOT / 'attention/teams-reader.json'
def resources_path(): return w.ROOT / 'auth/teams-reader-resources.json'
def unverified_path(): return w.ROOT / 'auth/teams-reader-unverified.json'
def resource_key(path): return w.digest(urllib.parse.unquote(path.split('?', 1)[0]).rstrip('/'))
def resource_holds(): return w.load(resources_path()) if resources_path().exists() else {}
def load_state(): return w.load(state_path()) if state_path().exists() else {'state': 'unknown', 'failures': 0}
def backoff_active(): return (load_state().get('retry_after') or 0) > time.time()


def remember_target():
    import auth_recovery
    target = auth_recovery.target()
    if not target:
        return
    with w.lock('teams-reader-state'):
        state = load_state()
        state.setdefault('affected', {})[target['kind'] + ':' + target['id']] = {**target, 'needs_resume': True}
        w.save(state_path(), state)


def held(kind, name): return bool(load_state().get('affected', {}).get(kind + ':' + name, {}).get('needs_resume'))


def hold_job(job):
    job.update(status='waiting', owner=None, teams_auth_hold=True, wake_at=time.time() + 300, updated_at=w.utc())


def reconcile(force=False):
    if load_state().get('state') != 'healthy':
        return
    candidates = [('job', job) for job in w.jobs() if job.get('teams_auth_hold')]
    candidates += [('intake', w.load(path)) for path in (w.ROOT / 'inbox').glob('*.json')
                   if w.load(path).get('status') == 'teams_auth_wait']
    for kind, old in candidates:
        with w.lock(('job-' if kind == 'job' else 'inbox-') + old['id'], nonblocking=True) as acquired:
            if not acquired:
                continue
            path = w.job_path(old['id']) if kind == 'job' else w.ROOT / 'inbox' / (old['id'] + '.json')
            with w.lock('registry'):
                current = w.load(path)
                pid, owner = current.get('worker_pid'), current.get('owner') or {}
                if current['status'] in ('paused', 'done', 'blocked', 'terminal'):
                    continue
                if kind == 'intake' and current['status'] != 'teams_auth_wait':
                    continue
                if pid and w.process_identity(pid) == current.get('worker_identity'):
                    continue
                if owner.get('pid') and w.process_identity(owner['pid']) == owner.get('identity'):
                    continue
                due = current.get('wake_at') if kind == 'job' else current.get('next_retry_epoch', 0)
                if not force and (due or 0) > time.time():
                    continue
                current.update(status='ready' if kind == 'job' else 'pending', teams_auth_hold=False,
                               owner=None, wake_at=None, next_retry_epoch=0, auth_resumed_at=w.utc())
                w.save(path, current)
            with w.lock('teams-reader-state'):
                state = load_state()
                entry = state.get('affected', {}).get(kind + ':' + old['id'])
                if entry:
                    entry.update(needs_resume=False, resumed_at=w.utc())
                    w.save(state_path(), state)


def resource_failure(paths):
    if len(paths) != 1:
        return
    with w.lock('teams-reader-state'):
        holds = resource_holds()
        holds[resource_key(paths[0])] = {'path': paths[0].split('?', 1)[0], 'classification': 'service_auth_required',
                                      'last_failure_at': w.utc(), 'retry_after': time.time() + 1800}
        w.save(resources_path(), holds)
        w.save(w.ROOT / 'attention/teams-reader-resources.json', {
            'state': 'partial_source_authentication', 'updated_at': w.utc(), 'held_sources': len(holds),
            'steps': ['Restore this source through the reader provider\'s normal interactive process.',
                      'Run agent-workflow teams-auth-check. Other sources remain eligible for silent reads.']})


def resource_success(paths):
    with w.lock('teams-reader-state'):
        for file in (resources_path(), unverified_path()):
            values = w.load(file) if file.exists() else {}
            for path in paths:
                values.pop(resource_key(path), None)
            if file.exists():
                w.save(file, values)
        notice = w.ROOT / 'attention/teams-reader-resources.json'
        if not resource_holds() and notice.exists():
            w.save(notice, {**w.load(notice), 'state': 'resolved', 'resolved_at': w.utc()})


def result_state(code, retry_after=300, message_read=False):
    with w.lock('teams-reader-state'):
        state, now = load_state(), time.time()
        if code == 79:
            count = state.get('rate_limit_count', 0) + 1
            delay = max(300, retry_after, min(3600, 300 * 2 ** min(count - 1, 4)))
            state.update(state='rate_limited', rate_limit_count=count, retry_after=now + delay,
                         reason='HTTP 429. Repeated sign-in does not repair throttling.')
        elif code == 76:
            count = state.get('transient_failures', 0) + 1
            state.update(state='retrying', transient_failures=count, retry_after=now + min(1800, 30 * 2 ** min(count, 6)),
                         reason='Shared silent-reader transport failure. Interaction is not established.')
        elif code in (4, 64, 77):
            state['last_resource_error_at'] = w.utc()
        elif code == 0:
            if state.get('state') != 'rate_limited' or (state.get('retry_after') or 0) <= now:
                state.update(state='healthy', failures=0, retry_after=None, reason=None, last_success_at=w.utc())
                if message_read:
                    state.update(last_message_read_success_at=w.utc(), transient_failures=0)
                if action_path().exists():
                    w.save(action_path(), {**w.load(action_path()), 'state': 'resolved', 'resolved_at': w.utc()})
        else:
            count = state.get('failures', 0) + 1
            human = code in (75, 78) or count >= 3
            state.update(state='needs_help' if human else 'retrying', failures=count, last_failure_at=w.utc(),
                         retry_after=now + (1800 if human else 60),
                         reason='interaction_required' if code == 75 else 'silent_reader_unavailable')
            if human:
                w.save(action_path(), {'state': 'needs_interactive_help', 'reason': state['reason'], 'updated_at': w.utc(),
                                      'human_login_required': code == 75, 'interactive_background_disabled': True,
                                      'steps': ['Use the configured reader provider\'s normal sign-in or repair procedure interactively.',
                                                'Run agent-workflow teams-auth-check. Never paste tokens into chat.']})
                if now - state.get('last_notice_epoch', 0) >= 86400:
                    w.notify('Teams reader needs operator help. Background login is disabled. See teams-auth-status.')
                    state['last_notice_epoch'] = now
        w.save(state_path(), state)
        return state


def safe_path(path):
    import teams_intake
    path = teams_intake.graph_path(path)
    route = path.split('?', 1)[0]
    decoded = urllib.parse.unquote(route)
    if any(part in ('.', '..') for part in decoded.split('/')) or '\\' in decoded or '#' in path:
        raise ValueError('Invalid Teams reader route')
    permitted = (r'/me', r'/me/chats(?:/[^/]+)?', r'/me/joinedTeams',
                 r'/chats/[^/]+/messages(?:/[^/]+)?(?:/replies(?:/[^/]+)?)?',
                 r'/teams/[^/]+/channels(?:/[^/]+(?:/messages(?:/[^/]+)?(?:/replies(?:/[^/]+)?)?)?)?')
    if not any(re.fullmatch(pattern, route) for pattern in permitted):
        raise ValueError('Teams reader route is outside identity, chat, and channel reads')
    return path


def command(args):
    config = configuration.intake('teams')
    reader = config.get('reader_command')
    if not reader or config.get('reader_contract') != {'version': 1, 'read_only': True, 'silent_only': True}:
        raise FileNotFoundError('No configured read-only, silent reader')
    configuration.intake('teams', {**config, 'enabled': True}, require_enabled=True)
    if not args or args[0] not in ('fetch', 'check'):
        raise ValueError('Only silent identity and read-only fetch are allowed')
    if args[1:3] != ['--account', config.get('account')]:
        raise ValueError('The configured operator account is required')
    if args[0] == 'fetch':
        if len(args) < 5 or args[3] != '--urls' or len(args[4:]) > 100:
            raise ValueError('A bounded explicit read-only URL list is required')
        args = [*args[:4], *(safe_path(path) for path in args[4:])]
    elif len(args) != 3:
        raise ValueError('Invalid silent identity request')
    if not Path(reader[0]).is_file():
        raise FileNotFoundError('Configured silent reader is missing')
    return [*reader, *args]


def verify_contract(reader):
    result = subprocess.run([*reader, '--background-policy'], stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=5, env=configuration.clean_worker_env(os.environ))
    if result.returncode:
        raise ValueError('Silent reader contract verification failed')
    data = json.loads(result.stdout)
    if data.get('version') != 1 or data.get('read_only') is not True or data.get('silent_only') is not True or data.get('interactive_fallback') is not False:
        raise ValueError('Reader does not verify the required background contract')


def pace():
    with w.lock('teams-reader-pace'):
        path = w.ROOT / 'auth/teams-reader-pace.json'
        due = w.load(path).get('next_at', 0) if path.exists() else 0
        if due > time.time():
            time.sleep(min(10, due - time.time()))
        w.save(path, {'next_at': time.time() + 10})


def run(args, force=False):
    paths = args[4:] if len(args) >= 4 and args[3] == '--urls' else []
    if not force and any((resource_holds().get(resource_key(path), {}).get('retry_after') or 0) > time.time() for path in paths):
        remember_target()
        print('TEAMS_READER_RESOURCE_AUTH_HELD: source deferred', file=sys.stderr)
        return 77
    state = load_state()
    if (state.get('retry_after') or 0) > time.time() and (not force or state.get('state') == 'rate_limited'):
        remember_target()
        code = 79 if state.get('state') == 'rate_limited' else 76 if state.get('state') == 'retrying' else 75
        print('TEAMS_READER_BACKGROUND_BACKOFF: see teams-auth-status', file=sys.stderr)
        return code
    try:
        argv = command(args)
    except ValueError:
        print('TEAMS_READER_POLICY_REJECTED: read-only contract rejected this command', file=sys.stderr)
        return 64
    except FileNotFoundError:
        result_state(78)
        print('TEAMS_READER_UNAVAILABLE: no interactive fallback', file=sys.stderr)
        return 78
    with ExitStack() as stack:
        if not any(stack.enter_context(w.lock('teams-reader-slot-' + str(index), nonblocking=True)) for index in range(2)):
            print('TEAMS_READER_BACKGROUND_BUSY: deferred without login', file=sys.stderr)
            return 76
        try:
            config = configuration.intake('teams')
            verify_contract(config['reader_command'])
        except (OSError, ValueError, subprocess.TimeoutExpired):
            result_state(78)
            print('TEAMS_READER_CONTRACT_FAILED: no interactive fallback', file=sys.stderr)
            return 78
        pace()
        if load_state().get('state') == 'rate_limited' and backoff_active():
            return 79
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=configuration.clean_worker_env(os.environ), start_new_session=True)
        def stop():
            if process.poll() is None:
                try: os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError: return
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        handlers = {}
        def cancel(_signal, _frame):
            stop()
            raise KeyboardInterrupt('Silent Teams read canceled')
        try:
            for sig in (signal.SIGINT, signal.SIGTERM): handlers[sig] = signal.signal(sig, cancel)
            try:
                out, err = process.communicate(timeout=38)
                code = process.returncode
            except subprocess.TimeoutExpired:
                stop()
                out, err, code = '', '', 76
        finally:
            stop()
            for sig, handler in handlers.items(): signal.signal(sig, handler)
        if code == 0:
            try:
                decoder, remaining, values = json.JSONDecoder(), out.lstrip(), []
                while remaining:
                    value, end = decoder.raw_decode(remaining)
                    if not isinstance(value, dict) or value.get('error') or value.get('isError'):
                        raise ValueError('Invalid reader result')
                    values.append(value)
                    remaining = remaining[end:].lstrip()
                if not values or (paths and len(values) != len(paths)):
                    raise ValueError('Incomplete reader response')
                if len(paths) == 1 and paths[0].split('?', 1)[0] == '/me':
                    data = values[0]
                    if data.get('id', '').lower() != config.get('self_id', '').lower() or data.get('userPrincipalName', '').lower() != config['account'].lower():
                        code = 78
            except (ValueError, AttributeError):
                code = 4
        if code == 77:
            remember_target()
            resource_failure(paths)
        if code in (75, 76, 78, 79): remember_target()
        retry = re.search(r'retry_after_seconds=(\d{1,7})', err)
        result_state(code, int(retry.group(1)) if retry else 300,
                     message_read=code == 0 and any('/messages' in path or '/replies' in path for path in paths))
        if code == 0:
            resource_success(paths)
            sys.stdout.write(out)
        else:
            print('TEAMS_READER_READ_FAILED: exit=' + str(code) + '; no interactive fallback', file=sys.stderr)
        return code


def status(details=False):
    state, holds = load_state(), list(resource_holds().values())
    unknown = w.load(unverified_path()) if unverified_path().exists() else {}
    result = {**state, 'held_source_count': len(holds), 'held_sources': holds if details else holds[:5],
              'held_sources_truncated': not details and len(holds) > 5, 'unverified_source_count': len(unknown),
              'message_access': 'representative_checks_passed' if state.get('last_health_check', {}).get('representative_data_verified') else 'not_verified',
              'all_sources_recovered': False if holds or unknown or state.get('state') != 'healthy' else None}
    if not details: result.pop('affected', None)
    return result


def data_canaries(config):
    import teams_intake, teams_coverage
    state = w.load(teams_intake.state_path()) if teams_intake.state_path().exists() else {}
    sources = list(state.get('channels', {}).values())[:1]
    catalog = w.ROOT / 'teams-chat-catalogue.json'
    if catalog.exists():
        chat = next((c for c in w.load(catalog).get('items', {}).values() if c.get('chat_type') == 'oneOnOne'), None)
        if chat: sources.append(chat)
    return [(source['kind'] + '_window', teams_coverage.first_path(config, source, {})) for source in sources]


def probe(path):
    config = configuration.intake('teams')
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(io.StringIO()):
        code = run(['fetch', '--account', config.get('account', ''), '--urls', path], force=True)
    try: data = json.loads(output.getvalue()) if code == 0 else None
    except ValueError: data = None
    return code, data


def health_check(details=False):
    config = configuration.intake('teams')
    code, data = probe('/me?$select=id,userPrincipalName')
    identity = bool(code == 0 and isinstance(data, dict) and data.get('id', '').lower() == config.get('self_id', '').lower()
                    and data.get('userPrincipalName', '').lower() == config.get('account', '').lower())
    canaries = data_canaries(config) if identity else []
    checks = []
    for kind, path in canaries:
        result, value = probe(path)
        checks.append({'kind': kind, 'exit': result, 'passed': result == 0 and isinstance(value, dict) and isinstance(value.get('value'), list)})
        if result in (76, 79): break
    data_ok = bool(checks and len(checks) == len(canaries) and all(item['passed'] for item in checks))
    with w.lock('teams-reader-state'):
        w.save(state_path(), {**load_state(), 'last_health_check': {'at': w.utc(), 'identity_verified': identity,
                                                                'representative_data_verified': data_ok, 'checks': checks}})
    if identity and data_ok: reconcile(force=True)
    return {'verified': identity and data_ok, 'identity_verified': identity, 'representative_data_verified': data_ok,
            'exit': 0 if identity and data_ok else code or next((item['exit'] for item in checks if item['exit']), 4),
            'silent_only': True, 'checks': checks, 'auth': status(details),
            'scope': 'Identity and representative message windows, not proof of complete source coverage'}


if __name__ == '__main__':
    os.umask(0o077)
    if sys.argv[1:] == ['--background-policy']:
        print(json.dumps({'version': 1, 'read_only': True, 'silent_only': True, 'interactive_fallback': False}))
    else:
        raise SystemExit(run(sys.argv[1:]))
