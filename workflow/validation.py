"""Source-bound automated validation. Human review gates remain separate."""


def github(data, required=None):
    """Require current successful checks. An absent suite is not a test pass."""
    head = data.get('head')
    if not head:
        raise ValueError('The tested GitHub source is unknown')
    latest = {}
    for check in data.get('checks', []):
        key = check.get('name')
        if key and check.get('id', 0) >= latest.get(key, {}).get('id', 0):
            latest[key] = check
    statuses = {}
    for value in data.get('statuses', []):
        key = value.get('context')
        if key and value.get('id', 0) >= statuses.get(key, {}).get('id', 0):
            statuses[key] = value
    names = set(required) if required is not None else set(latest) | set(statuses)
    if not names:
        raise ValueError('Automated validation is absent or unknown; no test pass is assumed')
    for name in names:
        check, status = latest.get(name), statuses.get(name)
        if check:
            if check.get('head_sha') != head or check.get('status') != 'completed' or check.get('conclusion') != 'success':
                raise ValueError('A required current-head GitHub check has not passed: ' + name)
        elif not status or status.get('head_sha') != head or status.get('state') != 'success':
            raise ValueError('A required current-head GitHub status has not passed: ' + name)
    return True


def ado_policy(policy, source, tested_source=None, inputs_unchanged=False, review=False):
    if not policy.get('blocking') or policy.get('type') != 'Build' or policy.get('state') == 'notApplicable':
        return True
    context = policy.get('context') or {}
    if policy.get('state') != 'approved' or context.get('isExpired'):
        return False
    if not context.get('buildIsNotCurrent'):
        return True
    # Source-only reuse needs explicit source and input evidence. A stale flag alone proves nothing.
    return bool(review and inputs_unchanged and tested_source == source
                and (policy.get('settings') or {}).get('queueOnSourceUpdateOnly') is True)


def tracked_builds(data):
    candidates = {value for value in (data.get('head'), data.get('merge_commit')) if value}
    for build in data.get('extra_builds', []):
        if build.get('status') != 'completed' or build.get('result') != 'succeeded':
            raise ValueError('A tracked required integration/E2E run has not passed')
        if not build.get('sourceVersion') or build['sourceVersion'] not in candidates:
            raise ValueError('A tracked run is not bound to the tested source or verified merge candidate')
    return True


def automation(data, job=None, review=False):
    job = job or {}
    if data.get('provider') == 'github' or 'checks' in data:
        github(data, job.get('required_checks'))
        return tracked_builds(data)
    policies = data.get('policies')
    if policies is None:
        raise ValueError('Automated validation is absent or unknown')
    evidence = job.get('source_only_validation', {})
    for policy in policies:
        entry = evidence.get(str(policy.get('id')), {})
        if not ado_policy(policy, data.get('head'), entry.get('source'), entry.get('inputs_unchanged') is True, review):
            raise ValueError('A required current-head ADO build has not passed')
    return tracked_builds(data)
