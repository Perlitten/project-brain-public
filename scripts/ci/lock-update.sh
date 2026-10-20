#!/usr/bin/env bash
set -euo pipefail
test "$CI_COMMIT_BRANCH" = main
case "$CI_PIPELINE_EVENT" in cron|deployment) ;; *) exit 2 ;; esac
: "${GH_TOKEN:?Woodpecker lock updater needs its own scoped GitHub token}"
apt-get update
apt-get install -y --no-install-recommends git curl ca-certificates gh
python -m pip install pip-tools
# gh is provisioned explicitly; do not pipe an unauthenticated installer to a shell.
command -v gh
PYTHON=python bash scripts/regen_locks.sh
python scripts/check_third_party_notices.py || echo "Inventory changed; the PR requires a notice review before CI can pass."
git config user.name woodpecker-ci
git config user.email 'woodpecker-ci@users.noreply.github.com'
git checkout -B deps/lock-update
git add requirements.lock requirements-dev.lock
git diff --cached --quiet && exit 0
git commit -m 'chore(deps): regenerate hashed requirements locks'
gh auth setup-git
git push --force-with-lease origin deps/lock-update
gh pr view deps/lock-update --repo "$CI_REPO" >/dev/null 2>&1 || \
  gh pr create --repo "$CI_REPO" --base main --head deps/lock-update \
  --title 'chore(deps): regenerate hashed requirements locks' \
  --body 'Regenerated hashed Python locks using scripts/regen_locks.sh under Woodpecker.'
