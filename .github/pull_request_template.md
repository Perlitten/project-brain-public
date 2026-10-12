## What changed

<!-- A short description of the change. -->

## Why

<!-- The problem this solves. Link the issue if there is one. -->

## How it was tested

<!-- Commands you ran and what you checked by hand. -->

## Checklist

- [ ] `ruff check .`, `bash scripts/mypy_gate.sh` and `pytest -q` pass
- [ ] `python scripts/check_migrations.py` passes (if the schema changed)
- [ ] `npm test` and `npm run typecheck` pass in `apps/web` (if the web UI changed)
- [ ] Tests added or updated for behaviour changes
- [ ] `CHANGELOG.md` updated under `Unreleased` (if user-visible)
- [ ] No secrets, `.env` contents or personal emails in code or commit messages
