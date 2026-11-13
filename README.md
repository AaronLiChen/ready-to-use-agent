# Ready-to-use agent

Portable configuration for coding agents, with safe installation and Pi native compaction.
The configuration contains no credentials, private conversations, or machine-specific runtime state.

## Included setup

- Shared task ownership, review, validation, and safety rules.
- ASD-STE100 writing principles for human-facing text.
- Global instruction profiles for Pi, Claude Code, and Codex.
- Pi native compaction for supported GPT Responses models.
- Bounded recovery for oversized histories and interrupted tool calls.
- Non-destructive installation, dry-run output, backups, and guarded uninstall.

Workflow automation is a separate module.
Its setup includes persistent PR ownership, rereview, reapproval, intake, and brief action notices.
Installation alone does not grant PR-write, messaging, cloud, or production authority.

## Quick start

Requirements: Python 3.9 or later, Node.js 22.19 or later, Git, and npm.
Node.js 24 is the recommended test runtime.

1. Clone this repository.

   ```bash
   git clone https://github.com/AaronLiChen/ready-to-use-agent.git
   cd ready-to-use-agent
   ```

2. Install the locked public dependencies without lifecycle scripts.

   ```bash
   npm ci --ignore-scripts
   ```

3. Preview the configuration changes.

   ```bash
   python3 install.py --agents pi claude codex --dry-run
   ```

4. Install the selected profiles.

   ```bash
   python3 install.py --agents pi claude codex
   ```

5. Start the locked Pi runtime.

   ```bash
   npx --no-install pi
   ```

6. Authenticate through Pi's `/login` command.

7. Select your provider and model through `/model`.

If you need only Pi, use `--agents pi`.
Keep this clone for `npx --no-install pi`.
The lockfile pins the validated runtime and patched dependency versions.
This installation does not upgrade an existing global Pi runtime.

Existing Pi sessions need `/reload` after installation.
New sessions load the global extension automatically.
Each machine needs its own normal authentication.
Do not copy credential files or full conversation history between machines or products.

### Optional startup model

The installer preserves existing model selection by default.
To select a startup model explicitly, supply both options:

```bash
python3 install.py --provider github-copilot --model gpt-6.1-sol
```

Use a model available to your own account.
This example does not guarantee model availability.

### Pi-package installation only

If you want only the Pi resources, use:

```bash
pi install git:github.com/AaronLiChen/ready-to-use-agent
```

This method loads native compaction and an on-demand setup skill.
It does not install other agent profiles or background services.
Do not combine this method with the full installer.
The installer rejects duplicate compaction-hook installation.

## Native compaction

Use `/native-compact status` to inspect activation.
Use `/native-compact recover` when an old session already exceeds the provider's input limit.

Supported provider IDs:

- `github-copilot`
- `openai`
- `openai-codex`
- `azure-openai-responses`

The model must use a Responses API and have an ID that starts with `gpt-`.
Other models keep Pi's normal compaction behavior.

The extension default is 200,000 tokens, or 70% of a smaller model's context window.
This is an extension default, not an OpenAI product default.

See [native compaction](docs/native-compaction.md) for commands, recovery behavior, and safeguards.

## Installation safety

The installer preserves unrelated instructions, model settings, native permissions, and folder-trust controls.
It refuses to replace unmanaged extensions or edited managed files.
It never imports authentication, session history, model catalogs, or active workflow jobs.
It does not start a scheduler or cloud task during installation.

Configuration and backups remain local under `~/.agents/ready-to-use-agent/`.
Each selected agent receives a managed instruction block.

Inspect installation state:

```bash
python3 install.py --doctor
```

Preview removal:

```bash
python3 install.py --uninstall --dry-run
```

Remove unchanged managed configuration:

```bash
python3 install.py --uninstall
```

The uninstall command preserves operator edits and restores original backed-up files when safe.

## Validation

```bash
npm test
npm run typecheck
python3 -m unittest discover -s tests -v
python3 scripts/check_export.py
npm audit --registry=https://registry.npmjs.org --audit-level=moderate
```

Default tests use synthetic data and local fake providers.
They do not use real provider credentials, write PR comments, approve PRs, or provision infrastructure.
CI validates macOS and Linux installation behavior.
Native Windows supports the core instruction and compaction files.
The persistent workflow controller requires a supported Unix environment, such as WSL2 on Windows.

A test pass does not certify model review quality or future production behavior.
See [security boundaries](docs/security.md) before activation.

## License

MIT. See [LICENSE](LICENSE).
This license does not cover external vendor binaries, internal skills, or proprietary SDKs.
