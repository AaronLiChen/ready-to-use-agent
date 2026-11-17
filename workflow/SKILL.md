---
name: portable-pr-workflow
description: Use the installed portable controller for authorized PR implementation, review, validation, and durable handoff.
license: MIT
---

# Portable PR workflow

Read repository instructions and applicable provider skills before work.
Use the operator's own authentication and native permission controls.
Installation grants no PR-write, messaging, cloud, or production authority.
Read the installed policy at `../policies/agent-setup.md` and writing policy at `../policies/writing.md`.
Resolve those paths from this skill directory.

## Ownership

Use the existing `agent-workflow` command. Do not start another watcher.
Keep one local writer per canonical PR and one owned worktree per task.
Do not infer another workstream from directories, timestamps, or historical transcripts.
Preserve existing external ownership exclusions and narrower task restrictions.
Keep ownership until the provider verifies actual merge or close.
Green CI and reviewer approval do not end the watch.

For enrollment, provide the goal, scope, exclusions, acceptance criteria, and owned worktree.
For GitHub, provide `--github-login` with the operator's own configured account.
Comments and approvals require separate explicit delegation.
Exact repository/login watch rules authorize repository-wide intake, not additional permissions or self-approval.
If a repository rule is removed or disabled, stop its repository-authorized writes.
Use `--allow-comments` or `--allow-approvals` only after the operator authorizes that task action.
Reviewer intake uses controller delegation. Intake workers cannot grant themselves additional permissions.
Do not modify task JSON or controller configuration directly from a bounded worker.

## Current-source review

Re-fetch identity, live source, target, all threads, reviews, and checks before each mutation.
Read resolved and reopened threads. Verify old findings against current code.
Use an independent clean checkout at the exact live source SHA.
After a rewrite, preserve dirty or stale worktrees. Prepare a new independent checkout when necessary.
Never edit or push another author's branch in review mode.
Never self-approve under any configured account.
Review changed code and author replies. Deduplicate substantive findings.
Reuse current validation when source and relevant inputs remain unchanged.
For valid ADO source-only policies, verify recorded source and actual test outcomes.
An unrelated target advance alone does not require another source-only build.
Record source-only policy proof with `review-evidence --source-only-evidence /absolute/source-proof.json`.
Verify actual tested source, outcomes, and unchanged inputs. Do not manufacture evidence to satisfy the guard.
Never label an older result a fresh current-target E2E pass.
An optional reproducer has one 90-second total attempt, including setup.

Before an authorized approval, record the genuine review:

```bash
agent-workflow review-evidence JOB --head SOURCE_SHA --evidence 'Actual code review and required validation evidence'
```

For a managed GitHub write, set `AUTOWORKFLOW_EXPECTED_HEAD` to the freshly verified source SHA.
Use an explicit PR URL or both the PR number and `--repo`.
Use `gh pr comment`, `review`, `ready`, or a supported title/body `edit` for managed GitHub writes.
The adapter pins reviews to the reviewed commit. It rejects raw API mutations, raw GraphQL, and credential-printing commands.
For ADO comments, use `agent-workflow ado-write --head SOURCE_SHA --comment-file /absolute/finding.txt`.
For ADO approval, use `agent-workflow ado-write --head SOURCE_SHA --approve` after genuine review and required validation.
For a reply, add `--reply-thread THREAD_ID`. For a new inline context, add `--thread-context /absolute/context.json`.
If a receipt is uncertain, inspect the provider before retrying. Do not duplicate a completed write.
The managed adapters reject unsupported mutations. They are not an OS sandbox or a semantic reviewer.
Never merge, waive checks, impersonate reviewers, or modify production through this workflow.

## Implementation and validation

Use reviewable changes, explicit-file commits, and normal non-force pushes.
Use `git push origin OWNED_BRANCH` from the enrolled worktree root.
Repair authorized required checker, integration, and E2E failures.
Do not duplicate matching queued or running validation.
Bind evidence to source, merge candidate, configuration, artifacts, fixtures, selection, and run IDs.
State each test result precisely. A skipped or incomplete test is not a pass.
After all required current-candidate automated validation passes, publish ready for review when authorized.
Preserve explicit draft-only and security holds.
Verify the same source SHA, non-draft state, and PR URL after publication.
Human reviewer and merge gates are not automated test failures.

## Action notices and recovery

After a confirmed review action, report its receipt:

```bash
agent-workflow action JOB --kind comment --head SOURCE_SHA --thread-id THREAD_ID --comment-id COMMENT_ID
agent-workflow action JOB --kind approval --head SOURCE_SHA
```

These commands verify evidence. They never post, approve, or merge.
If notice verification is deferred, continue the normal checkpoint. Do not repeat the provider write.
Keep private message text, tokens, and raw errors out of notices and public comments.
Unchanged polls must remain quiet.

On `AUTOWORKFLOW_AUTH_WAIT`, stop repeated token attempts and checkpoint waiting.
Use `agent-workflow auth-status` for ADO holds.
Only the operator may complete interactive sign-in, MFA, or permission approval.
Never clear caches, log out, switch tenants, or grant access from background work.
Teams reads require a configured read-only, silent reader. Never invoke an interactive fallback.

## Handoff

At an external wait, checkpoint the exact evidence and next action:

```bash
agent-workflow checkpoint JOB --status waiting --next 'Next useful action' --evidence 'Exact source and actual run/thread outcomes'
```

For completed implementation validation, add `--validation-complete --validated-head SOURCE_SHA`.
Track manually queued required ADO E2E with repeatable `--build-id`.
For a pending authorized write, add `--pending-action comment`, `approval`, or `publish`.
Use `--wake-after` only for dependencies absent from provider probes.
After handoff, stop foreground writes.
If safe work remains, use `--status ready`.
For a real boundary with no safe work, use `--status blocked --decision` with evidence, options, and recommendation.
Do not claim durable monitoring without an installed, enabled service and enrolled ownership.
