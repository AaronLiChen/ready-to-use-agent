# Repository instructions

Use short, direct American English. Use 20 words or fewer per sentence.
Keep exact code, identifiers, commands, paths, and required formal text unchanged.

Read `README.md` and the applicable files in `docs/` before changes.
Use `npm test`, `npm run typecheck`, and `python3 -m unittest discover -s tests -v` for validation.
Run `python3 scripts/check_export.py` before every commit and push.

Do not commit credentials, private conversations, session files, model catalogs, or machine-specific runtime state.
Do not copy internal skills, proprietary SDK binaries, or unrelated repository code.
Use synthetic test data. Live provider tests require separate opt-in and the operator's own account.
Do not change real home-directory configuration or start schedulers during tests.

Preserve existing instructions, permissions, authentication, and folder-trust controls.
Never self-approve, merge, bypass validation, or modify production without separate authority.
