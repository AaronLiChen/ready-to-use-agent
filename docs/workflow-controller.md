# Portable workflow controller

This dependent change adds the portable workflow module after the core installer and native-compaction setup.
The module is not implemented in this initial scaffold.
Keep this pull request as a draft until implementation and required validation are complete.

## Required module

- Durable task enrollment, ownership, checkpoint, and handoff.
- Isolated per-PR workers and one writer per PR.
- Parallel independent PR lifecycles through actual merge or close.
- Azure DevOps and GitHub direct-reviewer intake with explicit operator activation.
- Rereview after author changes and necessary current-head reapproval.
- Scoped validation evidence, including valid source-only policy results.
- Brief action notices and local action history.
- Bounded authentication recovery without background interactive login.
- An optional read-only, silent Teams reader interface.
- macOS launchd and Linux user-service installation with safe defaults.
- Pi ownership protection and Claude Stop/handoff hooks.

## Safety requirements

No credentials, conversations, active jobs, machine-specific state, internal skills, or vendor binaries belong in this module.
New installations must not start intake or grant PR/cloud authority implicitly.
Existing permissions, folder-trust decisions, user edits, and active task ownership must remain intact.

Native Windows requires a supported Unix environment, such as WSL2, for the persistent controller.
The core instruction and compaction setup remains separate from this platform requirement.

## Acceptance

The full installer must deploy a self-contained controller without a private repository dependency.
Tests must exercise isolated installation and real fake-worker handoff without cloud or PR writes.
Hosted macOS/Linux validation, dependency audit, and export-privacy checks must pass before publication.
