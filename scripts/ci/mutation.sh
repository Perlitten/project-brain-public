#!/usr/bin/env bash
set -euo pipefail
python scripts/ci/wait_services.py
python -m venv .venv
export PATH="$PWD/.venv/bin:$PATH"
python -m pip install --require-hashes -r requirements-dev.lock -r eval/mutation-requirements.lock
python -m pip install --no-deps -e .
PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE USER fixture WITH CREATEDB PASSWORD 'fixture_local_only'" \
  -c "CREATE DATABASE brain_team_test OWNER fixture"
mkdir -p .mutation-results/witnesses

export PYTHONPATH="$PWD/eval/mutation_support" PYTEST_PLUGINS=import_check PYTHONUNBUFFERED=1
export BRAIN_MUTATION_WITNESSES="$PWD/.mutation-results/witnesses"
shard="${SHARD:?}"
case "$shard" in [0-9]|[12][0-9]|3[01]) ;; *) exit 2 ;; esac
status=0
  mkdir -p ".mutation-results/shard-$shard"
  set +e

timeout --signal=INT --kill-after=30s 335m .venv/bin/python -m eval.project_mutation run \
  --inventory docs/testing/evidence/project-mutation-inventory.json \
  --shards 32 --shard "$shard" --output .mutation-results/shard-$shard/report.json \
  > .mutation-results/shard-$shard/mutmut.log 2>&1

  code=$?
  set -e
  tail -60 ".mutation-results/shard-$shard/mutmut.log"
  if [ "$code" -ne 0 ]; then status=1; fi
find mutants -name '*.meta' -print0 > .mutation-results/meta-files
tar --null -T .mutation-results/meta-files -czf .mutation-results/raw-metadata.tar.gz
tar -czf "mutation-shard-$shard.tar.gz" .mutation-results
exit "$status"
