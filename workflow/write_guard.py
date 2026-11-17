"""Fail-closed guards for managed PR writes. These guards are not an OS sandbox."""
import os
from pathlib import Path
import re
import urllib.parse

import configuration


def assigned():
    import workflow as w
    if os.environ.get('AUTOWORKFLOW_INTAKE'):
        raise ValueError('Preparation workers cannot write to PR providers')
    name = os.environ.get('AUTOWORKFLOW_JOB')
    if not name:
        raise ValueError('Managed PR writes require an enrolled task')
    job = w.load(w.job_path(name))
    if job.get('status') != 'active':
        raise ValueError('This task does not own an active write cycle')
    expected = ({'pid': job['worker_pid'], 'identity': job.get('worker_identity')}
                if job.get('worker_pid') else job.get('owner'))
    if not expected or w.owner() != expected:
        raise ValueError('This process does not own the active PR task')
    return job


def authorize(kind, head=None, job=None):
    import workflow as w
    job = assigned() if job is None else job
    if job.get('github_repository_watch'):
        import github_intake
        if not github_intake.enrolled_watch_authorized(job):
            raise ValueError('The repository-wide review rule was removed or disabled')
    permissions = configuration.delegation(job)
    if kind in ('comment', 'approval') and not permissions['comments' if kind == 'comment' else 'approvals']:
        raise ValueError('This background PR action has no explicit operator delegation')
    if kind == 'publish' and (job.get('mode') != 'implement' or not job.get('publish_after_validation')):
        raise ValueError('Only the owned implementation task may publish this PR')
    if job.get('mode') == 'review' and kind not in ('comment', 'approval'):
        raise ValueError('Review workers cannot change the author branch or PR metadata')
    data = w.snapshot(job)
    if data.get('state') not in ('open', 'active'):
        raise ValueError('The PR is no longer open')
    if not head or data.get('head') != head:
        raise ValueError('PR source changed or the exact reviewed head is absent')
    if job.get('expected_target') and data.get('target') != job['expected_target']:
        raise ValueError('PR target changed; reconfirm task scope')
    if job.get('intake'):
        request = w.load(w.ROOT / 'inbox' / (job['intake'] + '.json'))
        if request.get('source_kind') == 'github' and (job.get('reviewed_head') or job.get('last_reviewed_head')):
            pass  # GitHub consumes a submitted request. The enrolled review remains owned.
        else:
            adapter = w.intake_adapter(request)
            if not adapter.verify_request(w.load(adapter.config_path()), request):
                raise ValueError('The reviewer assignment is no longer verifiable')
    if kind == 'approval':
        if job.get('mode') != 'review' or job.get('reviewed_head') != head or job.get('blocking_findings'):
            raise ValueError('Approval requires a genuine current-head review with no blocking findings')
        if data.get('draft') is not False:
            raise ValueError('A draft PR cannot receive managed approval')
        author = data.get('author')
        if isinstance(author, str):
            config_path = w.ROOT / 'github-config.json'
            logins = w.load(config_path).get('logins', []) if config_path.exists() else []
            logins += [job.get('github_login', '')]
            if author.lower() in [value.lower() for value in logins]:
                raise ValueError('Self-approval is forbidden under every configured identity')
        else:
            import auth_recovery
            if (author or {}).get('uniqueName', '').lower() == auth_recovery.expected_account().lower():
                raise ValueError('Self-approval is forbidden')
            state = auth_recovery.load_state()
            if (author or {}).get('id') == state.get('expected_identity_id'):
                raise ValueError('Self-approval is forbidden')
    if kind in ('approval', 'publish'):
        import validation
        validation.automation(data, job, review=kind == 'approval')
    return data


def options(args, values, booleans=()):
    """Accept only known flags. Normalize attached values without ambiguous native parsing."""
    selected, positional = {}, []
    index = 0
    while index < len(args):
        value = args[index]
        if not value.startswith('-'):
            positional.append(value)
            index += 1
            continue
        key, separator, attached = value.partition('=')
        if key in booleans:
            if separator and attached != 'true':
                raise ValueError('Use an explicit supported Boolean flag')
            name = booleans[key]
            content = True
        else:
            if key not in values and not separator and value[:2] in values and not value.startswith('--'):
                key, separator, attached = value[:2], '=', value[2:]
            if key not in values:
                raise ValueError('Unsupported managed GitHub option: ' + key)
            name = values[key]
            if separator:
                content = attached
            else:
                index += 1
                if index >= len(args):
                    raise ValueError('Missing managed GitHub option value')
                content = args[index]
        if name in selected:
            raise ValueError('Repeated managed GitHub option: ' + name)
        selected[name] = content
        index += 1
    return positional, selected


def github_read(args):
    values = {'-X': 'method', '--method': 'method', '-q': 'jq', '--jq': 'jq',
              '-t': 'template', '--template': 'template', '--cache': 'cache', '--hostname': 'hostname'}
    flags = {value: value for value in ('--paginate', '--slurp', '--silent', '--include', '-i')}
    positional, selected = options(args, values, flags)
    if len(positional) != 1 or selected.get('method', 'GET').upper() not in ('GET', 'HEAD'):
        raise ValueError('Use scoped PR commands for managed API mutations')
    endpoint = positional[0]
    if selected.get('hostname', 'github.com').lower() != 'github.com':
        raise ValueError('The managed GitHub host must remain github.com')
    if '://' in endpoint:
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme != 'https' or parsed.hostname != 'api.github.com'
                or parsed.port not in (None, 443) or parsed.username or parsed.password):
            raise ValueError('The managed API host must remain api.github.com')
        endpoint = parsed.path
    if endpoint.startswith('//') or 'graphql' in urllib.parse.unquote(endpoint).split('?', 1)[0].lower().split('/'):
        raise ValueError('Managed raw GraphQL is unsupported. Use PR commands or REST reads.')


def github_command(args):
    """Return canonical scoped writes. Raw API mutations and interactive commands remain blocked."""
    if not args:
        raise ValueError('An explicit managed GitHub command is required')
    if any(value in ('--web', '-w', '--browser') or value.startswith(('--web=', '--browser=')) for value in args):
        raise ValueError('Managed GitHub commands cannot open an interactive browser')
    if args[0] == 'api':
        github_read(args[1:])
        return
    if args[0] in ('run', 'repo', 'issue', 'release') and args[1:2] in (['view'], ['list'], ['status']):
        return
    if args[0] != 'pr' or len(args) < 2:
        raise ValueError('Unsupported managed GitHub command. Do not print or change credentials.')
    operation = args[1]
    if operation in ('view', 'diff', 'checks', 'list', 'status'):
        return
    if operation not in ('comment', 'review', 'ready', 'edit'):
        raise ValueError('Managed workers cannot merge, close, or perform this PR mutation')
    values = {'--repo': 'repo', '-R': 'repo'}
    if operation in ('comment', 'review', 'edit'):
        values.update({'--body': 'body', '-b': 'body', '--body-file': 'body_file', '-F': 'body_file'})
    if operation == 'edit':
        values.update({'--title': 'title', '-t': 'title'})
    flags = ({'--approve': 'approve', '-a': 'approve', '--comment': 'comment', '-c': 'comment',
              '--request-changes': 'request_changes', '-r': 'request_changes'} if operation == 'review' else {})
    positional, selected = options(args[2:], values, flags)
    job = assigned()
    url = job.get('url', '')
    match = re.fullmatch(r'https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)', url)
    if not match:
        raise ValueError('Managed GitHub writes require an enrolled GitHub PR')
    repository = match[1] + '/' + match[2]
    if len(positional) != 1 or positional[0] not in (match[3], url):
        raise ValueError('The managed PR command must name its enrolled PR explicitly')
    if selected.get('repo', repository).lower() != repository.lower():
        raise ValueError('The GitHub repository differs from the enrolled task')
    if positional[0] != url and 'repo' not in selected:
        raise ValueError('Use the full enrolled PR URL or an explicit --repo for managed writes')
    if 'body_file' in selected:
        if 'body' in selected or selected['body_file'] == '-':
            raise ValueError('Use one explicit body or a local body file, not interactive input')
        with Path(selected['body_file']).open('rb') as stream:
            data = stream.read(1048577)
        if len(data) > 1048576:
            raise ValueError('The managed PR body file exceeds the supported size')
        selected['body'] = data.decode('utf-8')
    modes = [key for key in ('approve', 'comment', 'request_changes') if selected.get(key)]
    if operation == 'review' and len(modes) != 1:
        raise ValueError('Select exactly one explicit review mode')
    if operation == 'comment' and 'body' not in selected:
        raise ValueError('Managed PR comments require an explicit body')
    if operation == 'edit' and not any(key in selected for key in ('body', 'title')):
        raise ValueError('Select an explicit PR title or body edit')
    kind = ('approval' if modes == ['approve'] else 'comment') if operation == 'review' else {
        'comment': 'comment', 'ready': 'publish', 'edit': 'metadata'}[operation]
    head = os.environ.get('AUTOWORKFLOW_EXPECTED_HEAD') or job.get('reviewed_head') or job.get('validated_head')
    authorize(kind, head, job)
    if operation == 'review':
        event = {'approve': 'APPROVE', 'comment': 'COMMENT', 'request_changes': 'REQUEST_CHANGES'}[modes[0]]
        return ['api', 'repos/' + repository + '/pulls/' + match[3] + '/reviews', '--method', 'POST',
                '-f', 'event=' + event, '-f', 'commit_id=' + head, '-f', 'body=' + selected.get('body', '')]
    result = ['pr', operation, url]
    for key in ('body', 'title'):
        if key in selected:
            result += ['--' + key, selected[key]]
    return result


def azure_command(args):
    """Keep native ADO mutations outside the read-only CLI adapter."""
    if args[:1] == ['rest']:
        raise ValueError('Managed raw Azure REST is unsupported. Use provider reads or scoped ADO writes.')
    if args[:2] == ['devops', 'invoke']:
        values = {key: key for key in ('--area', '--resource', '--route-parameters', '--query-parameters',
                  '--api-version', '--org', '--organization', '--project', '--query', '--output', '-o',
                  '--accept-media-type', '--media-type', '--encoding', '--http-method')}
        for index, value in enumerate(args[2:-1], 2):
            if value in values and args[index + 1].startswith('-'):
                raise ValueError('Use explicit nonempty Azure CLI option values')
        _, selected = options(args[2:], values, {'--only-show-errors': '--only-show-errors'})
        if selected.get('--http-method', 'GET').upper() not in ('GET', 'HEAD'):
            raise ValueError('Use agent-workflow ado-write for managed ADO comments and approvals')
        return
    if args[:1] not in (['devops'], ['repos']):
        return
    readonly = (('repos', 'pr', 'show'), ('repos', 'pr', 'list'), ('repos', 'pr', 'policy', 'list'),
                ('repos', 'pr', 'reviewer', 'list'), ('repos', 'show'), ('repos', 'list'), ('repos', 'ref', 'list'),
                ('devops', 'project', 'show'), ('devops', 'project', 'list'))
    if any(tuple(args[:len(prefix)]) == prefix for prefix in readonly):
        return
    raise ValueError('Use scoped ADO writes. Other managed ADO mutations are unsupported.')


def git_push(args):
    """Permit only the enrolled implementation branch. Never permit a force push."""
    import workflow as w
    job = assigned()
    if job.get('mode') != 'implement':
        raise ValueError('Review workers cannot push the author branch')
    if any(value in ('--force', '-f', '--force-with-lease', '--mirror', '--all', '--delete') or value.startswith('+') for value in args):
        raise ValueError('Force, mirror, bulk, and destructive pushes are forbidden')
    if w.git(job['workspace'], 'branch', '--show-current') != job['branch']:
        raise ValueError('The owned source branch changed')
    if Path.cwd().resolve() != Path(job['workspace']).resolve():
        raise ValueError('Push requires the enrolled worktree root')
    if not job.get('enrolled_remote') or w.git(job['workspace'], 'remote', 'get-url', 'origin') != job['enrolled_remote']:
        raise ValueError('The enrolled Git origin changed')
    if args != ['push', 'origin', job['branch']]:
        raise ValueError('Use only git push origin followed by the enrolled source branch')
