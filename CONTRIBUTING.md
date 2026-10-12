# Contributing to Project Brain

Thanks for your interest. Bug reports, fixes and focused improvements are
welcome. For larger changes, open a feature request first so we can agree on
the approach.

## Development setup

Requirements: Docker with Compose, Python 3.12 (the version CI tests), and
Node 22 if you work on the web UI in `apps/web`.

```bash
git clone https://github.com/Perlitten/project-brain.git
cd project-brain
cp .env.example .env              # or run scripts/bootstrap.sh (POSIX shells)
docker compose up -d postgres redis neo4j
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

On Windows, use WSL2 or run the equivalent commands in PowerShell.

## Checks to run before a pull request

These are the same checks CI runs (`.github/workflows/ci.yml`):

```bash
ruff check .
bash scripts/mypy_gate.sh
python scripts/check_migrations.py
pytest -q
```

For changes in `apps/web` (Node 22):

```bash
cd apps/web
npm ci
npm test
npm run typecheck
```

## Commit identity and privacy check

CI runs `python scripts/check_public_privacy.py --allow-github-identities`.
It needs the full git history (not a shallow clone) and fails when:

- a commit author or committer email is not a GitHub noreply address
  (`<id>+<user>@users.noreply.github.com`);
- a commit message contains a personal email address.

Set your noreply address before committing:

```bash
git config user.email "<id>+<user>@users.noreply.github.com"
```

Never commit `.env` files, API keys or other secrets.

## Pull requests

- Target the `main` branch.
- Keep each pull request focused on one change and describe what changed, why,
  and how you tested it (the PR template has a checklist).
- Add or update tests for behaviour changes.
- Add a line to the `Unreleased` section of [`CHANGELOG.md`](CHANGELOG.md) for
  user-visible changes.

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE) and that you follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Report security issues as described in
[`SECURITY.md`](SECURITY.md), not in public issues.

New to the project? Prepared starter tasks live in
[`docs/good-first-issues.md`](docs/good-first-issues.md).
