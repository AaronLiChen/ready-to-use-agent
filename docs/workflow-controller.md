# Portable workflow controller

The optional module keeps scoped PR ownership until the provider verifies merge or close.
It supports implementation, review, rereview, current-head approval, validation repair, action history, and bounded authentication recovery.
Pi remains the scheduled coding backend.
Pi and Claude have native stop and handoff protection. Other frontends do not gain that protection automatically.

## Install without activation

Requirements: macOS or Linux, Python 3.9 or later, the locked Pi runtime, and the required provider CLIs.
For native Windows, use WSL2 for this module. The core profiles remain separate.

1. Install the repository's locked dependencies with `npm ci --ignore-scripts`.
2. Preview the module with `python3 install.py --agents pi claude --workflow --claude-hooks --dry-run`.
3. Install it with `python3 install.py --agents pi claude --workflow --claude-hooks`.
4. Keep this clone because the default scheduled Pi executable uses its locked runtime.
5. Authenticate through each provider's normal interactive procedure.
6. Select a model available to your own Pi account.

The `--claude-hooks` option is optional. It preserves existing hooks and native permissions.
The installer refuses unmanaged or edited controller files and competing legacy installations.
It does not copy credentials, conversations, active jobs, private skills, or vendor binaries.
It does not activate a service, reviewer intake, comments, approvals, or desktop notices.
The macOS service definition also has `Disabled` set to `true` before explicit activation.

Installed paths:

| Item | Destination |
| --- | --- |
| Controller source | `~/.agents/ready-to-use-agent/workflow/` |
| Private runtime state | `~/.agents/ready-to-use-agent/state/workflow/` |
| CLI | `~/.local/bin/agent-workflow` |
| Pi ownership extension | `~/.pi/agent/extensions/ready-agent-workflow.ts` |
| macOS service definition | `~/Library/LaunchAgents/org.ready-to-use-agent.workflow.plist` |
| Linux user units | `~/.config/systemd/user/ready-agent-workflow.{service,timer}` |

Use `--workflow-pi /absolute/path/to/pi` to select another destination Pi executable.
This option does not copy another machine's authentication or change the scheduled backend.

## Configure identity and authority

Before activation, edit the private configuration files under the runtime-state directory.
Use your own account values. The following values are synthetic examples.

For GitHub, configure `github-config.json`:

```json
{
  "enabled": false,
  "auto_start_reviews": false,
  "logins": ["ExampleOperator"],
  "repository_watches": []
}
```

Individual requests qualify across repositories accessible to the pinned account.
Team-only requests do not create individual assignments.
To authorize all open PRs in one repository, add an exact rule:

```json
{
  "repository": "example-org/example-repo",
  "login": "ExampleOperator",
  "enabled": true
}
```

Put the rule inside `repository_watches`.
Repository-wide intake does not require an individual review request.
The collector holds drafts, excludes all configured author identities, and deduplicates existing ownership.
Each repository scan has separate coverage. A failed scan does not withdraw pending work or stop another repository.
Repository-wide idle probes default to 15 minutes to reduce API pressure.
Direct requests and pending actions retain the normal probe and retry intervals.
Set `github_repository_probe_interval` in `scheduler-config.json` to adjust the repository-only cadence within 300–3,600 seconds.
To revoke repository-wide authority, remove or disable that exact rule.
Pausing intake alone does not revoke already-enrolled ownership.

For Azure DevOps, configure `ado-config.json`:

```json
{
  "enabled": false,
  "auto_start_reviews": false,
  "account": "reviewer@example.test",
  "organizations": ["example-org"]
}
```

Use the existing Azure CLI session. The controller does not start interactive background login.
The generic ADO helper uses standard public REST endpoints, not a private skill repository.

In `controller-config.json`, set `delegation.comments` or `delegation.approvals` only after you authorize those actions.
Both permissions default to `false`.
Manual enrollment also supports task-specific `--allow-comments` and `--allow-approvals` grants.
Preparation workers cannot grant themselves those permissions.
The controller preserves existing explicit task grants. Pause an owned task to stop its future work.
The installer grants no cloud, production, permission-change, or shared-resource deletion authority.

## Activate explicitly

1. Configure the required identities and optional action delegation.
2. Run `agent-workflow resume-github` or `agent-workflow resume-ado` for the required intake.
3. Run `agent-workflow service enable` to activate the installed user service.
4. Inspect `agent-workflow service status` and the provider inbox commands.

The service uses launchd on macOS and systemd user services on Linux.
Installation tests never activate real services.
The controller refuses service-label takeover and refuses removal while an active owner or worker remains.
An invalid intake does not stop independent provider intake or existing owned PR work.

To stop new discovery, use `pause-github`, `pause-ado`, or `pause-teams`.
These commands preserve enrolled watches.
Before service removal, checkpoint active owners and stop their writes.
Then run `agent-workflow service disable`.
Uninstall refuses an enabled controller or unfinished enrolled tasks.
Coordinate migration instead of importing another machine's jobs or deleting ownership records.

## Enroll and hand off

Create a clean, named, isolated worktree for the exact authorized PR before enrollment.
For GitHub, supply the pinned account:

```bash
agent-workflow start example-review --mode review \
  --url https://github.com/example-org/example-repo/pull/42 \
  --github-login ExampleOperator \
  --workspace /absolute/owned/review-worktree \
  --goal 'Review current source and follow later revisions until merge or close' \
  --scope 'This PR only. No author branch changes, merge, or unrelated infrastructure.' \
  --acceptance 'Verified review, applicable validation, and provider-confirmed terminal state'
```

Supply action-delegation flags only when the operator authorizes them.
For implementation, create the scoped draft PR before enrollment and use `--mode implement`.
Use `--keep-draft` for an explicit draft-only or security hold.

At an external wait, transfer ownership:

```bash
agent-workflow checkpoint example-review --status waiting \
  --next 'Review the next author revision or required validation result' \
  --evidence 'Exact source SHA, verified findings, and actual test or thread outcomes'
```

After handoff, stop foreground writes. The existing service owns future wakeups.
Use `--status ready` when safe independent work remains.
Use `--status blocked --decision` for a real boundary with evidence, options, and a recommendation.
Do not claim durable monitoring unless the service is enabled and the task is enrolled.

## Review evidence and managed writes

Before approval, inspect the exact live source, replies, resolved threads, reopened findings, and applicable validation.
Record the genuine result:

```bash
agent-workflow review-evidence example-review --head SOURCE_SHA \
  --evidence 'Actual code review and required current-candidate validation evidence'
```

For ADO source-only reuse, add `--source-only-evidence /absolute/source-proof.json`.
That file maps policy IDs to `source`, `inputs_unchanged`, and a nonempty `evidence` explanation.
Verify actual tested source, outcomes, and unchanged validation inputs before recording those claims.
The controller rejects stale source, expired policies, and policies without the source-only setting.
Do not call reused evidence a fresh current-target E2E pass.

Managed GitHub writes support `gh pr comment`, `review`, `ready`, and limited title/body `edit` operations.
Use the full enrolled PR URL or its number with an explicit matching `--repo`.
Set `AUTOWORKFLOW_EXPECTED_HEAD` to the freshly verified source SHA.
The adapter pins review submissions to that commit, including approvals.
It rejects raw API mutations, raw GraphQL, unsupported flags, interactive browser actions, and credential-printing commands.
REST reads remain available. Background merge and close commands remain unsupported.

For delegated ADO writes, use:

```bash
agent-workflow ado-write --head SOURCE_SHA --comment-file /absolute/finding.txt
agent-workflow ado-write --head SOURCE_SHA --comment-file /absolute/reply.txt --reply-thread 123
agent-workflow ado-write --head SOURCE_SHA --approve
```

For an inline finding, add `--thread-context /absolute/context.json` with the verified ADO file and line context.
The helper verifies task ownership, delegation, identity, live source, target, draft state, and applicable approval validation.
It keeps tokens in process memory and returns only a limited provider receipt.
ADO does not provide an atomic source-commit condition for votes. The controller must reassess later revisions.
If a receipt is uncertain, inspect provider state before retrying. Do not duplicate a completed write.
Native managed ADO mutations must not bypass this helper.

CLI guards are not an OS sandbox or a semantic reviewer.
Use an isolated host or operating-system account for untrusted PR validation.
Do not mount operator credentials into a test sandbox.

## Notices and authentication recovery

To enable action history, set `enabled` to `true` in `action-notices-config.json`.
To enable desktop notices, also set `desktop` to `true`.
Retain only the desired sources in its `sources` array.
Preparation and review notices report cycle starts. Approval notices require a verified provider vote.
Unchanged polls remain quiet.

```bash
agent-workflow activity --limit 20
agent-workflow activity --limit 20 --json
agent-workflow auth-status
agent-workflow auth-check
```

GitHub credentials stay pinned to one stored account. The controller does not switch the global account.
Authentication recovery uses bounded silent probes and resumes affected owned tasks after identity verification.
Only the operator can complete interactive sign-in, MFA, or permission approval.
Never paste tokens into chat or public comments.

## Optional read-only Teams interface

Teams intake stays disabled until you supply a reviewed reader and configure `teams-config.json`.
Supply `account`, `self_id`, an explicit `since` cutoff, and `reader_command` as an absolute command array.
Configure permitted PR organizations, GitHub owners, and display names for your own scope.
Set `reader_contract` to this object:

```json
{
  "version": 1,
  "read_only": true,
  "silent_only": true
}
```

The reader must support these commands:

```text
READER --background-policy
READER check --account ACCOUNT
READER fetch --account ACCOUNT --urls GRAPH_PATH [GRAPH_PATH ...]
```

For `--background-policy`, return one JSON object with the contract fields and `interactive_fallback: false`.
For fetch, return one JSON object per requested path, in request order, with whitespace between objects.
Use normal Graph object and collection shapes. Preserve continuation links for collection pagination.
Identity reads must return the configured `id` and `userPrincipalName`.

Exit codes: `0` success, `4` malformed data, `64` rejected route, `75` interaction required, and `76` transient failure.
Use `77` for source-specific access failure, `78` for unavailable reader, and `79` for throttling.
For throttling, stderr may contain `retry_after_seconds=N`. Do not include tokens or message content there.
The adapter restricts reads to identity, chat, channel, message, and reply routes.
It uses bounded concurrency, deadlines, backoff, and per-source holds. It never uses an interactive fallback.

Review the reader implementation before activation. A contract declaration does not prove that the implementation is safe.
The repository includes no proprietary reader, vendor SDK binaries, Teams posting interface, or copied authentication cache.

## Validation limits

The test suite uses isolated homes, synthetic providers, fake subprocesses, and inert service definitions.
Hosted validation covers macOS and Linux installation behavior, TypeScript, unit tests, dependency audit, and export privacy.
These results do not certify model review quality, a custom reader, or future production behavior.
See [security boundaries](security.md) before activation.
