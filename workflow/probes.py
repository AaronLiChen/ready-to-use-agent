"""Read-only PR snapshots through public, account-scoped provider helpers."""
import hashlib
import json
import re
from pathlib import Path
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import ado


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_json(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if result.returncode:
        # Do not log CLI stderr, which can include credentials/private responses.
        raise RuntimeError("Read-only provider CLI failed; check authentication interactively")
    return json.loads(result.stdout)


def clean_ado_token(output):
    """Keep warning lines out of HTTP headers without logging token material."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) == 1 and not any(ord(c) < 32 or ord(c) == 127 for c in lines[0]):
        return lines[0]
    candidates = [line for line in lines if re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", line)]
    if len(candidates) != 1:
        raise RuntimeError("Azure CLI returned ambiguous token output; no authenticated request sent")
    return candidates[0]


def acquire_ado_token(skill, context=None):
    import auth_recovery
    return auth_recovery.acquire(skill, context=context)


def ado_snapshot(job):
    skill = ado
    org, project, repo, number = skill.parse_pr_input(job["url"])
    try:
        token = acquire_ado_token(skill, {"kind": "job", "id": job["id"], "url": job["url"]} if job.get("id") else None)
    except SystemExit as exc:
        raise RuntimeError("ADO authentication unavailable; run az login interactively") from exc
    origin = "https://dev.azure.com/" + org + "/"

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect())

    def get(url, _token=None):
        if not url.startswith(origin):
            raise RuntimeError("Refusing ADO request outside the approved organization")
        result, seen = None, set()
        for _ in range(1000):
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
            try:
                with opener.open(req, timeout=45) as response:
                    data = json.load(response)
                    continuation = response.headers.get("x-ms-continuationtoken")
            except urllib.error.HTTPError as error:
                import auth_recovery
                context = {"kind": "job", "id": job["id"], "url": job["url"]} if job.get("id") else None
                auth_recovery.http_failure(error, context=context, resource_url=url)
            if not isinstance(data, dict) or ("message" in data and "typeKey" in data):
                raise RuntimeError("ADO returned an invalid/error response")
            if result is None:
                result = data
            elif isinstance(data.get("value"), list):
                result["value"].extend(data["value"])
            if not continuation:
                return result
            if continuation in seen or not isinstance(data.get("value"), list):
                raise RuntimeError("ADO pagination did not advance")
            seen.add(continuation)
            parts = urllib.parse.urlsplit(url)
            query = dict(urllib.parse.parse_qsl(parts.query))
            query["continuationToken"] = continuation
            url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
        raise RuntimeError("ADO pagination exceeded its safety bound")

    # Use a local helper instance. Concurrent snapshots must not replace global HTTP functions.
    from types import SimpleNamespace
    skill = SimpleNamespace(**{name: getattr(ado, name) for name in
                              ('api_get_pr', 'api_get_threads', 'api_get_policies')})
    from functools import partial
    def api_get_pr(org, project, repo, number, _token):
        return get(ado.api_base(org, project) + 'git/repositories/' + urllib.parse.quote(repo, safe='')
                   + '/pullRequests/' + str(number) + '?api-version=7.1')
    def api_get_threads(org, project, repo, number, _token):
        return get(ado.api_base(org, project) + 'git/repositories/' + urllib.parse.quote(repo, safe='')
                   + '/pullRequests/' + str(number) + '/threads?api-version=7.1')['value']
    def api_get_policies(org, project, project_id, number, _token):
        query = urllib.parse.urlencode({'artifactId': 'vstfs:///CodeReview/CodeReviewId/' + project_id + '/' + str(number),
                                      'api-version': '7.1-preview.1'})
        return get(ado.api_base(org, project) + 'policy/evaluations?' + query)['value']
    skill.api_get_pr, skill.api_get_threads, skill.api_get_policies = api_get_pr, api_get_threads, api_get_policies
    pr = skill.api_get_pr(org, project, repo, number, token)
    head = (pr.get("lastMergeSourceCommit") or {}).get("commitId")
    if not head or not pr.get("repository", {}).get("id") or pr.get("status") not in ("active", "completed", "abandoned"):
        raise RuntimeError("Incomplete ADO metadata; not proof of terminal/green state")
    result = {"provider": "ado", "state": pr["status"], "head": head,
              "base": (pr.get("lastMergeTargetCommit") or {}).get("commitId"),
              "source": pr["sourceRefName"], "target": pr["targetRefName"],
              "merge": pr.get("mergeStatus"), "draft": pr.get("isDraft"),
              "author": {k: (pr.get("createdBy") or {}).get(k) for k in ("id", "uniqueName", "displayName")},
              "reviewers": [{k: r.get(k) for k in ("id", "vote", "isRequired")} for r in pr.get("reviewers", [])]}
    if pr["status"] != "active":
        if job.get('include_terminal_threads'):
            result['threads'] = skill.api_get_threads(org, project, pr['repository']['id'], number, token)
        return result
    repo_id = pr["repository"]["id"]
    project_id = pr["repository"]["project"]["id"]
    result["threads"] = skill.api_get_threads(org, project, repo_id, number, token)  # includes resolved threads
    policies = skill.api_get_policies(org, project, project_id, number, token)
    result["policies"] = [{"id": p["evaluationId"], "state": p["status"],
                           "blocking": p["configuration"].get("isBlocking"),
                           "type": p["configuration"]["type"]["displayName"],
                           "settings": p["configuration"].get("settings"),
                           "context": p.get("context")} for p in policies]
    base = f"https://dev.azure.com/{org}/{urllib.parse.quote(project, safe='')}/_apis/"
    # A rejected source-only build may stay unchanged when an upstream baseline
    # fix merges. Observe the actual target ref in that state, not just ADO's
    # cached lastMergeTargetCommit. Do not wake on every unrelated target push
    # while normal CI is running/green.
    if pr.get("mergeStatus") in ("conflicts", "failure") or any(
            p["blocking"] and p["type"] == "Build" and p["state"] in ("rejected", "broken", "notStarted") for p in result["policies"]):
        query = urllib.parse.urlencode({"filter": pr["targetRefName"].removeprefix("refs/"), "api-version": "7.1"})
        refs = get(base + f"git/repositories/{repo_id}/refs?" + query)
        result["target_ref_head"] = next((r["objectId"] for r in refs.get("value", []) if r.get("name") == pr["targetRefName"]), None)
        if not result["target_ref_head"]:
            raise RuntimeError("Target ref unavailable; not proof an upstream fix was observed")
    statuses = get(base + f"git/repositories/{repo_id}/pullRequests/{number}/statuses?api-version=7.1")
    result["statuses"] = [{k: s.get(k) for k in ("id", "state", "context", "targetUrl", "description")} for s in statuses.get("value", [])]
    result["extra_builds"] = []
    for number in job.get("build_ids", []):
        b = get(base + f"build/builds/{int(number)}?api-version=7.1")
        result["extra_builds"].append({k: b.get(k) for k in ("id", "status", "result", "sourceVersion", "sourceBranch")})
    merge_id = (pr.get('lastMergeCommit') or {}).get('commitId')
    if (merge_id and re.fullmatch(r'[0-9a-fA-F]{40}', merge_id) and pr.get('mergeStatus') == 'succeeded'
            and any(build.get('sourceVersion') == merge_id and merge_id != head for build in result['extra_builds'])):
        commit = get(base + f'git/repositories/{repo_id}/commits/{merge_id}?api-version=7.1')
        if (commit.get('commitId') == merge_id and result.get('base')
                and len(commit.get('parents', [])) == 2 and set(commit['parents']) == {head, result['base']}):
            result['merge_commit'] = merge_id
    return result


def github_snapshot(job):
    import re
    match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", job["url"])
    owner, repo, number = match.groups()
    endpoint = f"repos/{owner}/{repo}"
    def gh_json(args):
        if job.get('github_login'):
            import github_auth
            return github_auth.run_json(args[1:], job['github_login'])
        return run_json(args)
    if job.get('github_login'):
        import github_auth
        identity = gh_json(['gh','api','user'])
        github_auth.verified(job['github_login'], identity)
        if job.get('github_user_id') and identity.get('id') != job['github_user_id']:
            raise github_auth.GitHubAuthError('GitHub job identity mismatch')

    def get(path):
        return gh_json(["gh", "api", path])

    def pages(path):
        data = gh_json(["gh", "api", "--paginate", "--slurp", path])
        return [item for page in data for item in page]

    pr = get(endpoint + f"/pulls/{number}")
    result = {"provider": "github", "state": "merged" if pr.get("merged") else pr["state"],
              "head": pr["head"]["sha"], "base": pr["base"]["sha"],
              "source": "refs/heads/" + pr["head"]["ref"], "target": "refs/heads/" + pr["base"]["ref"],
              "merge": pr.get("mergeable_state"), "draft": pr.get("draft"),
              "repository": (pr.get('base', {}).get('repo') or {}).get('full_name'),
              "source_repository": (pr.get('head', {}).get('repo') or {}).get('full_name'),
              "author": (pr.get("user") or {}).get("login"),
              "requested_reviewers": [r.get('login') for r in pr.get('requested_reviewers', [])]}
    if result["state"] != "open":
        return result
    # GraphQL includes resolution/reopening state; REST comments alone miss it.
    query = '''query($owner:String!,$repo:String!,$number:Int!,$cursor:String) {
      repository(owner:$owner,name:$repo) { pullRequest(number:$number) {
        reviewThreads(first:100,after:$cursor) {
          pageInfo { hasNextPage endCursor }
          nodes { id isResolved isOutdated path line
            comments(last:1) { totalCount nodes { id body updatedAt author { login } } }
          }
        }
      }}
    }'''
    cursor, threads = None, []
    for _ in range(1000):
        args = ["gh", "api", "graphql", "-f", "query=" + query, "-f", "owner=" + owner,
                "-f", "repo=" + repo, "-F", "number=" + number]
        if cursor:
            args += ["-f", "cursor=" + cursor]
        data = gh_json(args)
        if data.get("errors"):
            raise RuntimeError("GitHub review-thread query failed")
        page = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        threads.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        next_cursor = page["pageInfo"]["endCursor"]
        if not next_cursor or next_cursor == cursor:
            raise RuntimeError("GitHub pagination did not advance")
        cursor = next_cursor
    else:
        raise RuntimeError("GitHub pagination exceeded its safety bound")
    result["threads"] = threads
    for key, path in [("comments", f"/issues/{number}/comments"), ("inline_comments", f"/pulls/{number}/comments"),
                      ("reviews", f"/pulls/{number}/reviews")]:
        result[key] = [{**{k: c.get(k) for k in ("id", "body", "updated_at", "submitted_at", "state", "commit_id")},
                        'author': (c.get('user') or {}).get('login')} for c in pages(endpoint + path)]
    head = result["head"]
    checks = gh_json(["gh", "api", "--paginate", "--slurp", endpoint + f"/commits/{head}/check-runs?per_page=100"])
    result["checks"] = [{k: c.get(k) for k in ("id", "name", "head_sha", "status", "conclusion", "details_url")} for page in checks for c in page["check_runs"]]
    statuses = pages(endpoint + f"/commits/{head}/statuses?per_page=100")
    result["statuses"] = [{**{k: c.get(k) for k in ("id", "context", "state", "target_url")},
                           'head_sha': head} for c in statuses]
    return result


def snapshot(job):
    return github_snapshot(job) if job["url"].startswith("https://github.com/") else ado_snapshot(job)


def fingerprint(data):
    # Ignore provider bookkeeping churn, not comments/head/check state. Sort mapping
    # keys only: arrays are returned in provider order and are stable between reads.
    def stable(value):
        if isinstance(value, dict):
            return {k: stable(v) for k, v in value.items() if k not in (
                "_links", "url", "lastUpdatedDate", "publishedDate", "lastContentUpdatedDate",
                "startedDate", "completedDate", "queueTime", "finishTime", "startTime")}
        if isinstance(value, list):
            return [stable(v) for v in value]
        return value
    return digest(stable(data))
