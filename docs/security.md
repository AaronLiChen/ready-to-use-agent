# Security boundaries

This repository contains configuration and agent-controller source, not credentials or an execution sandbox.
Review the source before installation.

## Never export

- API keys, OAuth tokens, passwords, private keys, or credential stores.
- Full conversations, private messages, emails, session JSONL files, or encrypted checkpoints.
- Personal model catalogs, active workflow jobs, cloud state, or private action history.
- Internal repository code, private skills, or proprietary vendor binaries.

The export validator rejects obvious private state and common secret patterns.
It does not replace a human privacy review or a comprehensive secret scanner.

## Authority stays with the operator

Use each machine's own normal authentication.
Installation does not grant PR, messaging, cloud, or production authority.
Preserve the client's native permissions and folder-trust decisions.
Do not copy credentials or full history between products.

A review assignment does not prove that a PR deserves approval.
The agent must review the live head and verify required current-candidate validation.
It must never self-approve, impersonate another reviewer, bypass validation, or merge without separate authority.

Approval does not end the review watch.
A later source revision requires rereview, even when a positive vote remains present.

## External integrations

Azure DevOps and GitHub use the operator's own supported CLI authentication.
No token belongs in a repository file, command argument, log, or model-provider environment.

Teams requires a separate read-only, silent reader and the operator's own permitted access.
The public repository does not redistribute vendor SDK binaries or internal skills.
Background readers must not open sign-in dialogs or initiate consent.
If interaction is necessary, the controller must stop safely and request human help.

Any development/test provisioning needs task-scoped authority.
Production, shared-resource deletion, permissions, quotas, and identity safeguards remain separate boundaries.

## Existing environments

Do not install a competing controller over another machine's active PR ownership.
Coordinate migrations and confirm one writer per PR.
Never import another machine's active jobs merely to restore a dashboard.

The installer refuses unmanaged or edited file replacement.
Backups and installation manifests remain private and local.
Uninstall preserves operator edits instead of discarding them.
