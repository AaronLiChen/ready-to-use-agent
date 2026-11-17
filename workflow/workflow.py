#!/usr/bin/env python3
"""Durable PR ownership for installed frontends, with opt-in Pi workers.

Disabled background operation starts no provider or model calls.
The scheduler discovers only authorized intake and never grants merge authority.
"""
import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid

# Resolve symlinks so ~/.local/bin/agent-workflow finds its sibling module.
CODE = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE))
if __name__ == "__main__":
    sys.modules.setdefault("workflow", sys.modules[__name__])
from probes import digest, fingerprint, snapshot
from clients import harness_matches
import action_notices
import configuration
import shutil

ROOT = Path(os.environ.get("AUTOWORKFLOW_ROOT", str(configuration.DEFAULT_HOME / 'state/workflow')))
POLICY = Path(os.environ.get('AUTOWORKFLOW_POLICY', str(CODE / 'SKILL.md')))
PI = Path(os.environ.get('AUTOWORKFLOW_PI', configuration.controller().get('pi_executable') or shutil.which('pi') or 'pi'))
STATES = ("active", "ready", "waiting", "blocked", "paused", "done")
TERMINAL = ("merged", "closed", "completed", "abandoned")


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x") as out:
            os.chmod(tmp, 0o600)
            json.dump(data, out, indent=2)
            out.write("\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load(path):
    return json.loads(path.read_text())


def job_path(name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", name):
        raise ValueError("Job ID must be 1-80 lowercase letters/digits/hyphens")
    return ROOT / "jobs" / (name + ".json")


@contextmanager
def lock(name, nonblocking=False):
    path = ROOT / "locks" / (name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def jobs():
    return [load(p) for p in sorted((ROOT / "jobs").glob("*.json"))]


def git(workspace, *args):
    r = subprocess.run(["git", "-C", workspace, *args], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError("Git workspace check failed")
    return r.stdout.strip()


def process_identity(pid):
    r = subprocess.run(["/bin/ps", "-p", str(int(pid)), "-o", "lstart="], capture_output=True, text=True, timeout=5)
    return r.stdout.strip() if r.returncode == 0 else None


def owner():
    # Bind to the actual harness ancestor, not the short-lived bash tool shell.
    pid = os.getppid()
    for _ in range(15):
        r = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "ppid=,command="], capture_output=True, text=True, timeout=5)
        fields = r.stdout.strip().split(None, 1)
        if len(fields) != 2:
            break
        parent, command = fields
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = command.split()
        executable = Path(argv[0]).name if argv else ""
        native_harness = executable in ("pi", "claude", "claude-code")
        node_pi = executable in ("node", "bun") and any(
            "pi-coding-agent/dist/" in arg and arg.endswith((".js", ".mjs")) for arg in argv[1:5])
        if native_harness or node_pi or harness_matches(argv):
            return {"pid": pid, "identity": process_identity(pid)}
        pid = int(parent)
        if pid <= 1:
            break
    raise RuntimeError("Cannot identify an installed owning frontend; pass --owner-pid explicitly")


def canonical_url(value):
    value = value.strip().split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+", value):
        return value
    m = re.fullmatch(r"https://([^.\s/]+)\.visualstudio\.com/([^/\s]+)/_git/([^/\s]+)/pullrequest/(\d+)", value, re.I)
    if m:
        return "https://dev.azure.com/{}/{}/_git/{}/pullrequest/{}".format(*m.groups())
    if re.fullmatch(r"https://dev\.azure\.com/[^/\s]+/[^/\s]+/_git/[^/\s]+/pullrequest/\d+", value, re.I):
        return value
    raise ValueError("Provide a full HTTPS GitHub or Azure DevOps PR URL")


def notify(text):
    # Send brief local notices. Do not include private message text.
    path = ROOT / 'action-notices-config.json'
    if not path.exists():
        return False
    config = load(path)
    if config.get('enabled') is not True or config.get('desktop') is not True:
        return False
    if sys.platform != "darwin":
        if sys.platform.startswith('linux') and shutil.which('notify-send'):
            try:
                result = subprocess.run(['notify-send', 'Agent workflow', text[:220]],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                return result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                return False
        return False
    try:
        result = subprocess.run(["/usr/bin/osascript", "-e", 'on run argv\ndisplay notification (item 1 of argv) with title "Agent workflow"\nend run', text[:220]],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def legacy_watcher(url):
    # Preserve configured external ownership. Never infer authority from historical jobs.
    import parallel_dispatch
    for entry in configuration.controller().get('external_watches', []):
        if parallel_dispatch.pr_key(entry['url']) == parallel_dispatch.pr_key(url):
            return entry.get('owner', 'external controller')
    return None


def recent_cycles():
    values = [t for j in jobs() for t in j.get("cycles", [])]
    path = ROOT / "teams-worker-budget.json"
    if path.exists():
        values.extend(load(path).get("cycles", []))
    return [t for t in values if t > time.time() - 86400]


def scheduler_settings():
    defaults = {"max_parallel_workers": 4, "global_cycles_per_day": 48,
                "probe_interval": 300, "dispatch_interval": 5, "github_repository_probe_interval": 900}
    path = ROOT / "scheduler-config.json"
    configured = load(path) if path.exists() else {}
    result = {key: configured.get(key, value) for key, value in defaults.items()}
    bounds = {"max_parallel_workers": (1, 128), "global_cycles_per_day": (1, 100000),
              "probe_interval": (30, 3600), "dispatch_interval": (1, 60), "github_repository_probe_interval": (300, 3600)}
    for key, value in result.items():
        low, high = bounds[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError("Invalid scheduler setting: " + key)
    return result


def global_cycle_limit():
    return scheduler_settings()["global_cycles_per_day"]


def job_cycle_limit(job):
    return job.get('max_cycles_per_event',job['max_cycles_per_day']) if job.get('mode')=='review' else job['max_cycles_per_day']


def budget_cycles(job, now=None):
    now = time.time() if now is None else now
    credited = set(job.get('auth_cycle_times', []))
    values = [t for t in job.get('cycles', []) if t > now-86400 and t not in credited]
    if job.get('mode') == 'review' and job.get('attention_epoch'):
        # A new head, reply, resolution, or CI event gets a fresh bounded review
        # allowance. Old-revision/auth churn must not block a real new event.
        epochs = job.get('cycle_epochs', {})
        values = [t for t in values if epochs.get(str(t)) == job['attention_epoch']]
    return values


def budget_notice(job, until, global_limit=False):
    path = ROOT/'attention'/('global-model-budget.json' if global_limit else 'model-budget-'+job['id']+'.json')
    with lock('budget-attention'):
        previous = load(path) if path.exists() else {}
        episode = 'global' if global_limit else job.get('attention_epoch', 'rolling-day')
        record = {'state':'waiting_for_budget','at':utc(),'id':job['id'],'url':job['url'],
                  'episode':episode,'automatic_refill_at':until,
                  'reason':'Global model runaway guard' if global_limit else 'Repeated cycles for one unchanged review event',
                  'new_review_event_resets_local_allowance':not global_limit and job.get('mode')=='review'}
        save(path, record)
        if previous.get('episode')!=episode or previous.get('state')!='waiting_for_budget':
            notify('Agent workflow paused repeated model work at a runaway guard. Inspect parallel-status and attention records; new PR events remain monitored.')


def reserve_cycle(job=None):
    # All PR and preparation processes reserve under one short critical section.
    # A concurrency race must not lose usage or bypass the configured limit.
    with lock("model-budget"):
        now = time.time()
        global_cycles = recent_cycles()
        all_own = [t for t in (job or {}).get("cycles", []) if t > now - 86400]
        own = budget_cycles(job, now) if job else []
        limited = own if job and len(own) >= job_cycle_limit(job) else global_cycles
        if (job and len(own) >= job_cycle_limit(job)) or len(global_cycles) >= global_cycle_limit():
            if job:
                job.update(status="ready", budget_wait_until=min(limited) + 86400)
                save(job_path(job["id"]), job)
                budget_notice(job, job['budget_wait_until'], len(global_cycles)>=global_cycle_limit())
            return False
        if job:
            job["cycles"] = all_own + [now]
            if job.get('mode')=='review' and job.get('attention_epoch'):
                job['cycle_epochs']={key:value for key,value in job.get('cycle_epochs',{}).items() if float(key)>now-86400}
                job['cycle_epochs'][str(now)] = job['attention_epoch']
            save(job_path(job["id"]), job)
        else:
            path = ROOT / "teams-worker-budget.json"  # Shared legacy-named intake ledger.
            budget = load(path) if path.exists() else {"cycles": []}
            budget["cycles"] = [t for t in budget["cycles"] if t > now - 86400] + [now]
            save(path, budget)
        return True


def enroll(args):
    if os.environ.get("AUTOWORKFLOW_JOB"):
        raise ValueError("A scheduled worker cannot enroll nested workers")
    path = job_path(args.id)
    workspace = str(Path(args.workspace).expanduser().resolve())
    if git(workspace, "rev-parse", "--show-toplevel") != workspace:
        raise ValueError("Use the isolated worktree root, not a subdirectory")
    branch = git(workspace, "branch", "--show-current")
    if not branch or git(workspace, "status", "--porcelain"):
        raise ValueError("Enrollment requires a clean, named branch in an owned worktree")
    if args.mode != "review" and (branch in ("main", "master", "develop") or branch.startswith(("release/", "official/"))):
        raise ValueError("Implementation/E2E workers must use a non-protected source branch")
    identity = {"pid": args.owner_pid, "identity": process_identity(args.owner_pid)} if args.owner_pid else owner()
    if not identity["identity"]:
        raise ValueError("Owning harness process is not alive")
    settings = configuration.controller()
    url = canonical_url(args.url)
    intake = os.environ.get("AUTOWORKFLOW_INTAKE")
    if intake:
        if args.id != intake or args.mode != "review" or url != os.environ.get("AUTOWORKFLOW_INTAKE_URL"):
            raise ValueError("Review intake may enroll only its one verified review request")
        request = load(ROOT / "inbox" / (intake + ".json"))
        adapter = intake_adapter(request)
        config = load(adapter.config_path())
        if (not config.get("enabled") or not config.get("auto_start_reviews")
                or request.get("url") != url or request["status"] != "preparing"
                or not adapter.verify_request(config, request)):
            raise ValueError("Review request is paused or no longer verifiable; enrollment held")
    previous_watch = legacy_watcher(url)
    if previous_watch:
        raise ValueError("An existing watcher owns this PR; coordinate takeover first: " + previous_watch)
    remote = git(workspace, 'remote', 'get-url', 'origin')
    remote_key = digest([remote, branch])
    github_login = getattr(args, 'github_login', None) or os.environ.get('AUTOWORKFLOW_GITHUB_LOGIN') or settings.get('github_login')
    if url.startswith('https://github.com/'):
        import github_auth
        if not github_login:
            raise ValueError('GitHub tasks require an explicit --github-login or configured operator login')
        github_auth.state_path(github_login)
    permissions = configuration.delegation()
    if not intake:
        permissions = {key: permissions[key] or bool(getattr(args, 'allow_' + key, False)) for key in permissions}
    import parallel_dispatch
    with lock("registry"):
        if path.exists():
            raise ValueError("Job already exists; inspect/resume it instead of enrolling twice")
        for j in jobs():
            if j["status"] != "done" and (parallel_dispatch.pr_key(j["url"]) == parallel_dispatch.pr_key(url)
                                          or j["workspace"] == workspace or j["resource_key"] == remote_key):
                raise ValueError("PR/worktree/source branch already owned by job " + j["id"])
        data = {"id": args.id, "url": url, "mode": args.mode, "workspace": workspace, "branch": branch,
                "resource_key": remote_key, "goal": args.goal, "scope": args.scope,
                "acceptance": args.acceptance, "status": "active", "owner": identity,
                "enrolled_remote": remote, "delegation": permissions,
                "github_login": github_login if url.startswith('https://github.com/') else None,
                "session": os.environ.get("PI_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID"),
                "workstream": args.workstream or os.environ.get("PI_TERM_NAME") or os.environ.get("CLAUDE_TERM_NAME") or "autoworkflow-" + args.id,
                "created_at": utc(), "updated_at": utc(), "head_at_enrollment": git(workspace, "rev-parse", "HEAD"),
                "provider": os.environ.get("PI_PROVIDER") or settings.get("defaultProvider"),
                "model": os.environ.get("PI_MODEL") or settings.get("defaultModel"),
                "cycle_timeout": args.cycle_timeout, "max_cycles_per_day": args.max_cycles_per_day,
                "publish_after_validation": args.mode == "implement" and not getattr(args, "keep_draft", False),
                "next_action": "Continue authorized work, then checkpoint/handoff", "evidence": [], "cycles": []}
        if args.mode=='review':data['max_cycles_per_event']=getattr(args,'max_cycles_per_event',None) or args.max_cycles_per_day
        if intake:
            data["intake"] = intake
            data["intake_source"] = request.get("source_kind", "teams")
            if request.get('github_login'):
                data.update(github_login=request['github_login'], github_user_id=request.get('github_user_id'))
                repository_watch=request.get('assignment',{}).get('repository_watch')
                if repository_watch:data['github_repository_watch']=repository_watch
        save(path, data)
    return data


def publication_checkpoint(job, args):
    """Verify the publication part of an agent's current-head validation claim.

    This is not a substitute for the agent verifying required integration/E2E
    evidence; it checks live head/draft state and attached blocking build policies.
    """
    if not getattr(args, "validation_complete", False):
        return {}
    if job["mode"] != "implement":
        raise ValueError("Publication checkpoints apply only to owned implementation PRs")
    head = getattr(args, "validated_head", None)
    if not head or not re.fullmatch(r"[0-9a-fA-F]{40}", head):
        raise ValueError("--validation-complete requires --validated-head with the tested source SHA")
    observed_job = job
    if getattr(args, "build_id", None):
        observed_job = {**job, "build_ids": list(dict.fromkeys(args.build_id))}
    data = snapshot(observed_job)
    if str(data.get("head", "")).lower() != head.lower():
        raise ValueError("PR head changed after validation; revalidate before publishing/claiming completion")
    for policy in data.get("policies", []):
        if not policy.get("blocking") or policy.get("type") != "Build" or policy.get("state") == "notApplicable":
            continue
        context = policy.get("context") or {}
        if policy.get("state") != "approved" or context.get("isExpired") or context.get("buildIsNotCurrent"):
            raise ValueError("A required current-head build is not green/current; validation is not complete")
    import validation
    validation.tracked_builds(data)
    if 'checks' in data or data.get('provider') == 'github':
        import validation
        validation.github(data, job.get('required_checks'))
    publish = job.get("publish_after_validation", True)
    if publish and data.get("draft") is not False:
        raise ValueError("Validation is complete but the PR is still draft/unverified: publish it for review and re-fetch before handoff; explicit draft-only tasks must use --keep-draft")
    return {"validated_head": head.lower(), "publication": "ready_for_review" if data.get("draft") is False else "draft_hold",
            "publication_verified_at": utc()}


def checkpoint(args):
    path = job_path(args.id)
    worker = os.environ.get("AUTOWORKFLOW_JOB")
    intake = os.environ.get("AUTOWORKFLOW_INTAKE")
    if intake and intake != args.id:
        raise ValueError("Review preparation may hand off only its one enrolled review")
    if worker and worker != args.id:
        raise ValueError("Worker cannot checkpoint another job")
    # The scheduler holds this job lock while its worker runs. Only that worker
    # may write its checkpoint under the registry lock; foreground owners must
    # not race a poll/launch or overwrite a live worker's state.
    ownership_lock = nullcontext(True) if worker == args.id else lock("job-" + args.id, nonblocking=True)
    with ownership_lock as acquired:
        if not acquired:
            raise ValueError("Scheduler/worker is using this job; coordinate before checkpointing")
        with lock("registry"):
            j = load(path)
            session = os.environ.get("PI_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
            if session and j.get("session") and session != j["session"] and worker != j["id"]:
                raise ValueError("Job belongs to a different session; do not steal ownership")
            if j.get("worker_pid") and worker != j["id"] and process_identity(j["worker_pid"]) == j.get("worker_identity"):
                raise ValueError("A live worker owns this job; coordinate before checkpointing")
            if j["status"] in ("paused", "done"):
                raise ValueError("Paused/terminal job cannot be silently reactivated")
            publication = publication_checkpoint(j, args)
            j.update(status=args.status, next_action=args.next, evidence=args.evidence, updated_at=utc(), **publication)
            j["owner"] = None  # Atomic handoff: the main agent must stop mutating this worktree.
            j["last_checkpoint_cycle"] = os.environ.get("AUTOWORKFLOW_CYCLE")
            pending_action = getattr(args, "pending_action", None)
            wake_after = args.wake_after or (300 if pending_action and args.status == "waiting" else None)
            j["wake_at"] = time.time() + wake_after if wake_after else None
            if pending_action:
                j["pending_action"] = pending_action
            else:
                j.pop("pending_action", None)
            if args.build_id:
                j["build_ids"] = list(dict.fromkeys(args.build_id))
            if args.decision:
                j["decision"] = args.decision
            else:
                j.pop("decision", None)
            save(path, j)
    if args.status == 'blocked':
        action_notices.safe(action_notices.emit, j, 'blocked', [j.get('last_checkpoint_cycle'), j.get('decision')])
    return j


def control(name, action):
    if os.environ.get("AUTOWORKFLOW_JOB") or os.environ.get("AUTOWORKFLOW_INTAKE"):
        raise ValueError("Scheduled workers may checkpoint only; no ownership/control changes")
    job_path(name)  # Validate before constructing a lock path.
    with lock("job-" + name, nonblocking=True) as acquired:
        if not acquired:
            raise ValueError("Scheduler/worker is using this job; retry after its bounded cycle")
        with lock("registry"):
            path = job_path(name)
            j = load(path)
            if j.get("worker_pid") and process_identity(j["worker_pid"]) == j.get("worker_identity"):
                raise ValueError("Worker is running; stop/coordinate it before changing ownership")
            if j["status"] == "done":
                raise ValueError("Terminal job is retained for audit, not resumed")
            if action == "claim":
                new_owner = owner()
                existing = j.get("owner")
                if existing and existing != new_owner and process_identity(existing["pid"]) == existing["identity"]:
                    raise ValueError("Another live foreground harness owns this job; coordinate handoff first")
                j.update(status="active", owner=new_owner, session=os.environ.get("PI_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID"))
            else:
                j.update(status="paused" if action == "pause" else "ready", owner=None)
            j["updated_at"] = utc()
            save(path, j)
    return j


def guard(session=None, pid=None):
    def matches(j):
        if session and j.get("session"):
            return j["session"] == session
        return bool(pid and (j.get("owner") or {}).get("pid") == pid)
    return [j for j in jobs() if j["status"] == "active" and j.get("owner") and matches(j)
            and (not pid or j["owner"]["pid"] == pid)]


def workspace_problem(j):
    try:
        if git(j["workspace"], "branch", "--show-current") != j["branch"]:
            return "Worktree branch changed; refusing to overwrite or switch it"
        if git(j["workspace"], "status", "--porcelain"):
            return "Worktree is dirty; inspect interrupted/unowned changes before resuming"
        for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
            path = git(j["workspace"], "rev-parse", "--git-path", name)
            if (Path(j["workspace"]) / path).exists():
                return "Unfinished Git operation: " + name
        for name in ("rebase-merge", "rebase-apply"):
            path = git(j["workspace"], "rev-parse", "--git-path", name)
            if (Path(j["workspace"]) / path).exists():
                return "Unfinished Git rebase"
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return "Workspace unavailable; no repair attempted"
    return None


def worker_prompt(j, snapshot_path, cycle):
    import prompts
    return prompts.worker(j, snapshot_path, cycle)


def worker_command(j):
    command = [str(PI), '--offline', '--no-extensions', '--no-prompt-templates', '--no-themes',
               *configuration.worker_extensions(), '--tools', 'read,bash,edit,write',
               '--session-dir', str(ROOT / 'sessions' / j['id']),
               '--name', 'autoworkflow-' + j['id'], '-p']
    for flag, key in (('--provider', 'provider'), ('--model', 'model')):
        value = j.get(key) or configuration.controller().get('default' + key.title())
        if value:
            command += [flag, value]
    return command


def run_worker(j, snapshot_path):
    configuration.require_background()
    if not reserve_cycle(j):
        return False
    path = job_path(j["id"])
    cycle = uuid.uuid4().hex
    command = worker_command(j)
    import auth_recovery
    env = auth_recovery.worker_env(os.environ.copy())
    import github_auth
    import teams_reader
    env = github_auth.worker_env(env, j.get('github_login'))
    for k in ("PI_SESSION_ID", "PI_SESSION_FILE", "PI_TERM_NAME", "CLAUDE_SESSION_ID", "CLAUDE_TERM_NAME"):
        env.pop(k, None)
    env.update(AUTOWORKFLOW_JOB=j["id"], AUTOWORKFLOW_CYCLE=cycle,
               PI_TERM_NAME=j.get("workstream", "autoworkflow-" + j["id"]))
    log_path = ROOT / "logs" / (j["id"] + "-" + cycle + ".log")
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    prompt = worker_prompt(j, snapshot_path, cycle)
    with log_path.open("w") as out:
        proc = subprocess.Popen(command, cwd=j["workspace"], env=env, stdin=subprocess.PIPE,
                                stdout=out, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        j.update(status="active", worker_pid=proc.pid, worker_identity=process_identity(proc.pid), owner=None)
        save(path, j)
        action_notices.safe(action_notices.review_started, j, snapshot_path)
        def stop_child():
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
        old = {}
        def cancelled(signum, _frame):
            stop_child()
            raise KeyboardInterrupt("Scheduler stopped")
        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                old[sig] = signal.signal(sig, cancelled)
            proc.communicate(prompt, timeout=j["cycle_timeout"])
        except subprocess.TimeoutExpired:
            stop_child()
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
            stop_child()
            # Respect a worker-written handoff; reject a missing/failed checkpoint.
            latest = load(path)
            latest.update(last_worker_log=str(log_path), last_worker_exit=proc.returncode, owner=None, updated_at=utc())
            latest.pop("worker_pid", None)
            latest.pop("worker_identity", None)
            if (proc.returncode != 0 or latest.get('last_checkpoint_cycle') != cycle) and teams_reader.held('job',j['id']):
                teams_reader.hold_job(latest)
                auth_recovery.credit_cycle(latest,j['cycles'][-1])
            elif proc.returncode != 0 or latest.get("last_checkpoint_cycle") != cycle:
                if auth_recovery.held("job", j["id"]) or github_auth.held('job', j['id']):
                    if github_auth.held('job', j['id']):github_auth.hold_job(latest)
                    else:auth_recovery.hold_job(latest, model=True)
                    auth_recovery.credit_cycle(latest, j['cycles'][-1])
                    latest["auth_interrupted_worker"] = True
                else:
                    latest.update(status="blocked", decision="Worker failed/timed out or omitted its checkpoint; inspect " + str(log_path))
                    if action_notices.safe(action_notices.enabled, latest):
                        action_notices.safe(action_notices.emit, latest, 'blocked', ['worker', cycle])
                    else:
                        notify(latest["decision"])
            elif latest["status"] in ("waiting", "ready", "active") and (auth_recovery.held("job", j["id"]) or github_auth.held('job', j['id'])):
                if github_auth.held('job', j['id']):github_auth.hold_job(latest)
                else:auth_recovery.hold_job(latest, model=True)
                auth_recovery.credit_cycle(latest, j['cycles'][-1])
            if latest['status'] in ('waiting','ready','active') and teams_reader.held('job',j['id']):
                teams_reader.hold_job(latest)
            save(path, latest)
    return True


def tick_one(name):
    with lock("job-" + name, nonblocking=True) as acquired:
        if not acquired:
            return
        path = job_path(name)
        j = load(path)
        if j['url'].startswith('https://github.com/') and not j.get('github_login') and j.get('intake'):
            routing_path = ROOT/'inbox'/(j['intake']+'.json')
            if routing_path.exists():
                routing = load(routing_path)
                if routing.get('github_login'):
                    j.update(github_login=routing['github_login'], github_user_id=routing.get('github_user_id'))
                    save(path, j)
        if j["status"] in ("paused", "done", "blocked"):
            return
        import auth_recovery
        if not auth_recovery.may_run("job", name):
            return
        import github_auth
        if not github_auth.may_run('job', name):return
        o = j.get("owner")
        if o and process_identity(o["pid"]) == o["identity"]:
            return  # The foreground agent still owns the worktree.
        if j.get("worker_pid") and process_identity(j["worker_pid"]) == j.get("worker_identity"):
            return
        if j.get('github_repository_watch'):
            import github_intake
            if not github_intake.enrolled_watch_authorized(j):
                j.update(status='blocked',decision='Repository-wide review authorization was removed or disabled; stop this scoped review.',updated_at=utc())
                save(path,j)
                notify('GitHub repository-review authorization changed. Inspect '+name+' before any review write.')
                return
        with lock("resource-" + j["resource_key"], nonblocking=True) as acquired:
            if not acquired:
                return
            if j["status"] == "active":
                j["status"] = "ready"  # Harness died; rehydrate from durable state, never its transcript.
            try:
                data = snapshot(j)
                key = fingerprint(data)
                snap_path = ROOT / "snapshots" / (name + ".json")
                if snap_path.exists():
                    action_notices.safe(lambda: action_notices.seed(j, load(snap_path)))
                save(snap_path, data)
                if not data.get("head") or not data.get("state"):
                    raise RuntimeError("Incomplete provider snapshot")
                action_notices.safe(action_notices.observe, j, data)
                previous = j.get("fingerprint")
                j.pop("probe_error", None)
                j.pop('auth_probe_deferred', None)
                j.pop('probe_retry_at', None)
                j.update(fingerprint=key, last_poll_at=utc())
                if j['mode']=='review':j['attention_epoch']=key
                if data["state"] in TERMINAL:
                    j.update(status="done", terminal_state=data["state"], updated_at=utc(), owner=None)
                    save(path, j)
                    if not action_notices.safe(action_notices.enabled, j):
                        notify(name + ": PR " + data["state"])
                    return
                due = bool(j.get("wake_at") and j["wake_at"] <= time.time())
                changed = key != previous
                save(path, j)
                if j["status"] != "ready" and not changed and not due:
                    return
                problem = workspace_problem(j)
                if j["mode"] != "review" and data.get("source") != "refs/heads/" + j["branch"]:
                    problem = "PR source ref differs from the enrolled owned branch"
                if j.get("expected_target") and j["expected_target"] != data.get("target"):
                    problem = "PR target ref changed; re-confirm scope before continuing"
                j.setdefault("expected_target", data.get("target"))
                if problem:
                    j.update(status="blocked", decision=problem, updated_at=utc())
                    save(path, j)
                    if action_notices.safe(action_notices.enabled, j):
                        action_notices.safe(action_notices.emit, j, 'blocked', ['worktree', problem])
                    else:
                        notify(name + ": " + problem)
                    return
                # Bounded effort, not a silent task expiry. Budget automatically refills.
                j["cycles"] = [t for t in j["cycles"] if t > time.time() - 86400]
                global_cycles = recent_cycles()
                effective_cycles = budget_cycles(j)
                if len(effective_cycles) >= job_cycle_limit(j) or len(global_cycles) >= global_cycle_limit():
                    limiting_cycles = effective_cycles if len(effective_cycles) >= job_cycle_limit(j) else global_cycles
                    j.update(status="ready", budget_wait_until=min(limiting_cycles) + 86400)
                    save(path, j)
                    budget_notice(j, j['budget_wait_until'], len(global_cycles)>=global_cycle_limit())
                    return
                j.pop("budget_wait_until", None)
                attention_path=ROOT/'attention'/('model-budget-'+j['id']+'.json')
                if attention_path.exists():
                    attention=load(attention_path);attention.update(state='resolved',resolved_at=utc());save(attention_path,attention)
                return bool(run_worker(j, snap_path))
            except (Exception, SystemExit) as exc:
                # No URLs/headers/response bodies/CLI stderr in errors or notifications.
                latest = load(path)
                if isinstance(exc, github_auth.GitHubAuthError):
                    github_auth.hold_job(latest)
                    latest.update(probe_error='GitHub sign-in held; inspect github-inbox', last_poll_at=utc())
                    save(path, latest)
                    return
                if isinstance(exc, auth_recovery.AuthError):
                    if exc.reason == 'busy' and auth_recovery.load_state().get('state') == 'healthy':
                        latest.update(auth_probe_deferred=True, probe_retry_at=time.time()+30, last_poll_at=utc())
                        save(path, latest)
                        return
                    auth_recovery.hold_job(latest)
                    latest.update(probe_error="Authentication held; see agent-workflow auth-status", last_poll_at=utc())
                    save(path, latest)
                    return
                error = "Provider/scheduler error (" + type(exc).__name__ + "); check local auth and logs"
                if latest.get("probe_error") != error:
                    notify(name + ": " + error)
                latest.update(probe_error=error, last_poll_at=utc())
                save(path, latest)


def intake_adapter(request):
    kind = request.get("source_kind", "teams")
    if kind == "ado":
        import ado_intake
        return ado_intake
    if kind == "github":
        import github_intake
        return github_intake
    if kind == "teams":
        import teams_intake
        return teams_intake
    raise ValueError("Unsupported review intake source")


def prepare_intake():
    import teams_intake
    import ado_intake
    path = ROOT / "intake-scheduler-state.json"
    previous = load(path).get("last_source") if path.exists() else None
    sources = [("teams", teams_intake), ("ado", ado_intake)]
    if previous == "teams":
        sources.reverse()
    for name, adapter in sources:
        if adapter.prepare_one():
            save(path, {"last_source": name, "at": utc()})
            return True
    return False


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("start", help="Enroll an authorized, isolated PR task (foreground owned initially)")
    e.add_argument("id")
    for name in ("url", "workspace", "goal", "scope", "acceptance"):
        e.add_argument("--" + name, required=True)
    e.add_argument("--mode", choices=("implement", "review", "e2e"), required=True)
    e.add_argument("--owner-pid", type=int)
    e.add_argument('--github-login', help='Pin this task to one configured GitHub account')
    e.add_argument('--allow-comments', action='store_true', help='Explicitly delegate background comments for this task')
    e.add_argument('--allow-approvals', action='store_true', help='Explicitly delegate genuine background approvals for this review')
    e.add_argument("--workstream", help="Explicit workstream identity; defaults to the named tab or a new managed-job identity")
    e.add_argument("--keep-draft", action="store_true", help="Preserve an explicit draft-only/security publication hold")
    e.add_argument("--cycle-timeout", type=int, default=1800, choices=range(60, 7201), metavar="SECONDS")
    e.add_argument("--max-cycles-per-day", type=int, default=12, choices=range(1, 49), metavar="N", help='Implementation/E2E daily guard; legacy review-event fallback')
    e.add_argument('--max-cycles-per-event', type=int, choices=range(1,49), metavar='N', help='Review runaway limit for one unchanged attention event')
    c = sub.add_parser("checkpoint", help="Atomically hand off; stop foreground writes after this")
    c.add_argument("id")
    c.add_argument("--status", choices=("ready", "waiting", "blocked"), required=True)
    c.add_argument("--next", required=True)
    c.add_argument("--evidence", action="append", required=True)
    c.add_argument("--decision")
    c.add_argument("--wake-after", type=int, choices=range(300, 86401), metavar="SECONDS")
    c.add_argument("--pending-action", choices=("comment", "approval", "publish"), help="Retry an unfinished authorized write even if provider content does not change")
    c.add_argument("--build-id", type=int, action="append")
    c.add_argument("--validation-complete", action="store_true", help="Claim current-candidate required validations complete and verify publication")
    c.add_argument("--validated-head", help="Source SHA actually validated; required with --validation-complete")
    for action in ("pause", "resume", "claim"):
        sub.add_parser(action).add_argument("id")
    sub.add_parser("list")
    services = sub.add_parser('service', help='Inspect or explicitly activate installed user services')
    services.add_argument('action', choices=('status', 'enable', 'disable'))
    ado_write = sub.add_parser('ado-write', help='Post an explicitly delegated, source-verified ADO comment or approval')
    ado_write.add_argument('--head', required=True)
    operation = ado_write.add_mutually_exclusive_group(required=True)
    operation.add_argument('--approve', action='store_true')
    operation.add_argument('--comment-file')
    ado_write.add_argument('--reply-thread', type=int)
    ado_write.add_argument('--thread-context', help='JSON file with the verified inline file and line context')
    review = sub.add_parser('review-evidence', help='Record a genuine current-source review before approval')
    review.add_argument('id')
    review.add_argument('--head', required=True)
    review.add_argument('--evidence', required=True, action='append')
    review.add_argument('--blocking-findings', action='store_true')
    review.add_argument('--source-only-evidence', type=Path, help='JSON map of ADO policy IDs to tested source and verified input evidence')
    activity = sub.add_parser('activity', help='Show brief local review action history without provider or model calls')
    activity.add_argument('--limit', type=int, default=20, choices=range(1, 201), metavar='N')
    activity.add_argument('--json', action='store_true')
    action = sub.add_parser('action', help='Verify and announce one completed action for the owned review')
    action.add_argument('id')
    action.add_argument('--kind', required=True, choices=('comment', 'approval'))
    action.add_argument('--head', required=True)
    action.add_argument('--thread-id')
    action.add_argument('--comment-id')
    sub.add_parser("inbox", help="Show Teams-triggered review intake and coverage")
    sub.add_parser("ado-inbox", help="Show organization-wide ADO reviewer assignments and coverage")
    sub.add_parser("github-inbox", help="Show GitHub direct reviewer requests and per-account coverage")
    sub.add_parser("pause-github", help="Pause new GitHub reviewer intake/preparation")
    sub.add_parser("resume-github", help="Resume GitHub reviewer intake/preparation")
    sub.add_parser("pause-ado", help="Pause ADO assignment intake/preparation, not enrolled PR watches")
    sub.add_parser("resume-ado", help="Resume authorized ADO reviewer-assignment intake")
    sub.add_parser("pause-teams", help="Pause new Teams intake/preparation, not already enrolled PR watches")
    sub.add_parser("resume-teams", help="Resume verified Teams review-request intake")
    sub.add_parser("tick", help="Dispatch independent PR pipelines concurrently; one writer per PR")
    sub.add_parser("parallel-status", help="Show concurrent supervisors, direct workers, and configured capacity")
    sub.add_parser('retry-intake', help='Retry a blocked idle preparation without duplicating an enrolled watch').add_argument('id')
    sub.add_parser('teams-auth-status', help='Show bounded silent WorkIQ health; use details only when needed').add_argument('--all-held-sources', action='store_true')
    sub.add_parser('teams-auth-check', help='Verify identity and representative full message windows silently').add_argument('--all-held-sources', action='store_true')
    sub.add_parser("auth-status", help="Show authentication holds and required human reauthentication steps")
    sub.add_parser("auth-check", help="Verify restored ADO authentication and resume affected work")
    cycle = sub.add_parser("_cycle", help=argparse.SUPPRESS)
    cycle.add_argument("kind", choices=("job", "intake"))
    cycle.add_argument("id")
    cycle.add_argument("--reservation", required=True)
    g = sub.add_parser("guard", help="Report foreground jobs missing a durable handoff")
    g.add_argument("--session")
    g.add_argument("--pid", type=int)
    args = p.parse_args()
    if args.command == "start":
        result = enroll(args)
    elif args.command == "checkpoint":
        if args.status == "blocked" and not args.decision:
            p.error("blocked requires --decision with evidence/options/recommendation")
        result = checkpoint(args)
    elif args.command in ("pause", "resume", "claim"):
        result = control(args.id, args.command)
    elif args.command == 'service':
        import service
        result = service.status() if args.action == 'status' else service.change(args.action)
    elif args.command == 'ado-write':
        import ado_write
        result = ado_write.execute(args)
    elif args.command == 'review-evidence':
        job = load(job_path(args.id))
        worker = os.environ.get('AUTOWORKFLOW_JOB')
        if job['mode'] != 'review' or job['status'] != 'active' or (worker and worker != args.id):
            raise ValueError('Only the active owning review may record review evidence')
        if os.environ.get('AUTOWORKFLOW_INTAKE') or (not worker and (job.get('owner') or {}) != owner()):
            raise ValueError('This session does not own the review')
        data = snapshot(job)
        if data.get('head') != args.head or git(job['workspace'], 'rev-parse', 'HEAD') != args.head:
            raise ValueError('Review evidence requires the exact live source checkout')
        source_only = load(args.source_only_evidence) if args.source_only_evidence else {}
        policies = {str(policy['id']): policy for policy in data.get('policies', [])}
        if not isinstance(source_only, dict):
            raise ValueError('Source-only evidence must contain a policy map')
        for policy_id, proof in source_only.items():
            policy = policies.get(policy_id, {})
            if (not isinstance(proof, dict) or proof.get('source') != args.head
                    or proof.get('inputs_unchanged') is not True or not proof.get('evidence')
                    or policy.get('state') != 'approved' or (policy.get('context') or {}).get('isExpired')
                    or (policy.get('settings') or {}).get('queueOnSourceUpdateOnly') is not True):
                raise ValueError('Source-only reuse requires the tested source, unchanged inputs, and an approved unexpired policy')
        with lock('registry'):
            current = load(job_path(args.id))
            current.update(reviewed_head=args.head, review_evidence=args.evidence, blocking_findings=args.blocking_findings,
                           source_only_validation=source_only)
            save(job_path(args.id), current)
        result = {'id': args.id, 'reviewed_head': args.head, 'blocking_findings': args.blocking_findings}
    elif args.command == 'action':
        result = action_notices.report(args)
    elif args.command == 'activity':
        result = action_notices.history(args.limit)
        if not args.json:
            print('\n'.join(r['at'] + '  ' + r['text'] for r in result) or 'No review action notices recorded.')
            return
    elif args.command == "guard":
        result = [{k: j.get(k) for k in ("id", "status", "goal", "next_action")} for j in guard(args.session, args.pid)]
    elif args.command in ("pause-teams", "resume-teams", "pause-ado", "resume-ado", "pause-github", "resume-github"):
        if os.environ.get("AUTOWORKFLOW_JOB") or os.environ.get("AUTOWORKFLOW_INTAKE"):
            raise ValueError("A scoped worker cannot change review intake authority")
        kind = args.command.rsplit('-', 1)[-1]
        adapter = intake_adapter({"source_kind": kind})
        with lock(kind + "-config"):
            config = load(adapter.config_path())
            enabled = args.command.startswith("resume-")
            if enabled:
                configuration.intake(kind, {**config, 'enabled': True}, require_enabled=True)
            config.update(enabled=enabled, auto_start_reviews=enabled)
            save(adapter.config_path(), config)
        result = {kind + "_intake_enabled": config["enabled"], "existing_pr_watches": "unchanged"}
    elif args.command == "inbox":
        import teams_intake
        result = {"requests": [{k: r.get(k) for k in ("id", "url", "status", "job", "decision", "last_error", "context_error", "resolved_urls")} for r in teams_intake.inbox()],
                  "coverage": teams_intake.coverage_summary()}
    elif args.command in ("ado-inbox", "github-inbox"):
        adapter = intake_adapter({'source_kind': args.command.split('-', 1)[0]})
        result = {"requests": [{k: r.get(k) for k in ("id", "url", "status", "job", "github_login", "last_error")} for r in adapter.inbox()],
                  "coverage": adapter.coverage_summary()}
    elif args.command=='retry-intake':
        import worker_recovery
        result=worker_recovery.retry_intake(args.id)
    elif args.command in ('teams-auth-status', 'teams-auth-check'):
        import teams_reader
        result = teams_reader.health_check(details=args.all_held_sources) if args.command=='teams-auth-check' else teams_reader.status(details=args.all_held_sources)
    elif args.command in ("auth-status", "auth-check"):
        import auth_recovery
        if args.command == "auth-check":
            if os.environ.get("AUTOWORKFLOW_JOB") or os.environ.get("AUTOWORKFLOW_INTAKE"):
                raise ValueError("Background workers cannot initiate interactive authentication recovery")
            result = auth_recovery.tick(force=True)
        else:
            result = auth_recovery.status()
    elif args.command in ("tick", "_cycle", "parallel-status"):
        if os.environ.get("AUTOWORKFLOW_JOB") or os.environ.get("AUTOWORKFLOW_INTAKE"):
            raise ValueError("A scoped worker cannot start or control other PR pipelines")
        import parallel_dispatch
        if args.command == "tick":
            parallel_dispatch.run()
            return
        if args.command == "_cycle":
            parallel_dispatch.execute(args.kind, args.id, args.reservation)
            return
        result = parallel_dispatch.status()
    else:
        result = [{k: j.get(k) for k in ("id", "mode", "url", "status", "next_action", "decision", "probe_error", "auth_hold", "auth_reason", "pending_action", "budget_wait_until", "last_poll_at")} for j in jobs()]
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, FileNotFoundError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
