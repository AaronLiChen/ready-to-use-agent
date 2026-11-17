"""Public Azure DevOps URL and REST helpers. Tokens remain in process memory."""
import json
import re
import subprocess
import urllib.parse
import urllib.request

RESOURCE = '499b84ac-1321-427f-aa17-267ca6975798'


def organization(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,62}', value):
        raise ValueError('Invalid ADO organization')
    return value


def parse_pr_input(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ValueError('Provide an HTTPS Azure DevOps PR URL')
    parts = parsed.path.strip('/').split('/')
    if parsed.hostname == 'dev.azure.com' and len(parts) == 7 and parts[2] == '_git' and parts[4] == 'pullrequest':
        # Reject malformed routes before any authentication attempt.
        raise ValueError('Invalid Azure DevOps PR route')
    if parsed.hostname == 'dev.azure.com' and len(parts) == 6 and parts[2] == '_git' and parts[4] == 'pullrequest':
        org, project, _, repo, _, number = parts
    elif parsed.hostname and parsed.hostname.endswith('.visualstudio.com') and len(parts) == 5 and parts[1] == '_git' and parts[3] == 'pullrequest':
        org = parsed.hostname[:-len('.visualstudio.com')]
        project, _, repo, _, number = parts
    else:
        raise ValueError('Provide a full Azure DevOps PR URL')
    org = organization(org)
    project, repo = urllib.parse.unquote(project), urllib.parse.unquote(repo)
    if any(not p or p in ('.', '..') or '/' in p or '\\' in p or any(ord(c) < 32 for c in p) for p in (project, repo)):
        raise ValueError('Invalid Azure DevOps project or repository')
    if not number.isdigit() or int(number) <= 0:
        raise ValueError('Invalid Azure DevOps PR number')
    return org, project, repo, int(number)


def get_token():
    """Use the operator's existing Azure CLI session. Never start login."""
    import auth_recovery
    result = subprocess.run([auth_recovery.real_azure_cli(), 'account', 'get-access-token',
                             '--resource', RESOURCE, '--query', 'accessToken', '-o', 'tsv'],
                            capture_output=True, text=True, check=True, timeout=12)
    return result.stdout


def origin(org):
    return 'https://dev.azure.com/' + organization(org) + '/'


def api_base(org, project):
    return origin(org) + urllib.parse.quote(project, safe='') + '/_apis/'


def _get(url, token):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
        return json.load(response)


def api_get_pr(org, project, repo, number, token):
    return _get(api_base(org, project) + 'git/repositories/' + urllib.parse.quote(repo, safe='')
                + '/pullRequests/' + str(int(number)) + '?api-version=7.1', token)


def api_get_threads(org, project, repo, number, token):
    data = _get(api_base(org, project) + 'git/repositories/' + urllib.parse.quote(repo, safe='')
                + '/pullRequests/' + str(int(number)) + '/threads?api-version=7.1', token)
    if not isinstance(data.get('value'), list):
        raise RuntimeError('Missing Azure DevOps review threads')
    return data['value']


def api_get_policies(org, project, project_id, number, token):
    query = urllib.parse.urlencode({'artifactId': 'vstfs:///CodeReview/CodeReviewId/' + project_id + '/' + str(int(number)),
                                   'api-version': '7.1-preview.1'})
    data = _get(api_base(org, project) + 'policy/evaluations?' + query, token)
    if not isinstance(data.get('value'), list):
        raise RuntimeError('Missing Azure DevOps policy evaluations')
    return data['value']
