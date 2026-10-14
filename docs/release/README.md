# Release verification and records

Published artifacts are immutable versioned evidence, not a substitute for
installation verification. The recommended newcomer path is the tagged source
checkout in [Getting started](../GETTING_STARTED.md).

The source version may be a newer candidate than the latest published release.
Prepare consistent next-version markers in a reviewed release PR, so the
immutable source archive agrees with its own release. Keep the current `main`
markers until anonymous download and installation verification; merge the
release documentation after that verification. Never retag an existing version
or replace its artifacts.

## Verify a candidate locally

Use Python 3.12 and Node.js 22 with disposable test data. Start Postgres, Redis
and Neo4j using the documented source workflow, then activate `.venv`.

```bash
ruff check .
python scripts/check_public_launch.py
python scripts/check_public_privacy.py --allow-github-identities
bash scripts/mypy_gate.sh
python scripts/check_migrations.py
pytest -q
```

The public launch check also verifies current documentation links, versions,
installation entry points and published-tag clone instructions. Commit privacy
requires the complete Git history. Migration checking covers fresh creation
and idempotent re-application; use an isolated database, never operator data.

For the web app:

```bash
cd apps/web
npm ci
npm test
npm run typecheck
npm run build
npm run test:production
```

Record exit codes, exact commit, tool versions and test counts. Record every
skip and its reason. A skipped Docker/native-client test is not a passed
integration; supplement it with the relevant real invocation when possible.
Run browser checks for desktop and mobile, service failures, synthetic-data
labels and reduced motion. Screenshots must come from the tested implementation.

## Verify distribution contents

In an isolated build environment with `build` and `twine` installed:

```bash
python -m build
python -m twine check dist/*
python scripts/check_release_archives.py dist/*.whl dist/*.tar.gz
```

The wheel should include intended Python runtime modules and required resources,
not the Next.js development tree, local `.env`, environments or generated
artifacts. The source archive should retain Compose, bootstrap, current docs,
frontend source and its lockfile for a complete source installation.

Install the wheel in a fresh virtual environment, run `brain --help`, check
package version and inspect the MCP entry point. Extract the source archive
into a separate directory and verify bootstrap and frontend source are present.
Do not rely on the existing editable checkout to prove a wheel installation.

## Verify the complete user workflow

From a fresh public clone at the candidate's exact reviewed commit:

1. Bootstrap with no paid model calls; check datastore and provider status.
2. Index a clean sample checkout and run API, worker and dashboard.
3. Execute a real MCP `search_code` call and task-context request with the
   indexed canonical repository path; inspect returned source references.
4. Build/start the dashboard, leave ignored generated outputs in place, and
   reindex. Commit a legitimate disposable source change and reindex again.
5. Verify changed source is discoverable and generated/private paths are absent.
6. Stop/restart services and repeat health plus a representative MCP call.
7. Verify a clean source reinstall preserves required configuration and data;
   use disposable volumes for destructive reset testing only.

Mock embeddings prove wiring, not meaningful semantic ranking or coding
improvements. Any real-provider evaluation needs its own explicit model,
configuration, data and cost evidence.

## Publish through the verified pipeline

The release workflow is [`.github/workflows/release.yml`](../../.github/workflows/release.yml).
Its preflight rejects arbitrary dispatch refs, tag/package-version mismatches,
missing, pending or failed main-push CI for the exact tagged commit, and tags
with an existing GitHub Release. CI and CodeQL still run separately before
tagging; publication does not rerun those suites or itself enforce CodeQL.
All build jobs check out the verified commit, not a mutable tag lookup.

The wheel and source archive are built once and passed to publication as
checksum-verified artifacts. Image builds require commit/source/deployment
identity; images are inventoried and their revision labels checked before
registry push. Four SPDX inventories cover source, installed API packages,
installed worker packages and the web image. Missing backend dependencies fail
the release. Download failures or byte changes fail closed instead of causing
a second build. Registry image pushes precede the final GitHub Release step;
an interrupted run can leave GHCR tags without a matching Release. Do not
rewrite a published tag or claim atomic cross-registry publication. None of
these checks establishes real-provider usefulness.

Merge the runtime candidate only with green required CI and CodeQL. Prepare
final version markers in the release-documentation PR and require green CI and
CodeQL on that head too. Tag that reviewed commit, matching package metadata
and changelog, and use the existing release workflow. Merge the documentation
after public download/install verification. Do not rewrite a tag to recover a
failed job. Recover by rerunning the authorized pipeline or a new version,
preserving existing public evidence.

After publication, use an unauthenticated client to download the wheel, source
archive, SBOM and `SHA256SUMS` from GitHub. Verify all listed hashes and repeat
fresh wheel/source installation and MCP smoke checks. Record release URL,
tag/SHA, asset names and results before marking the release verified.

Update README, FAQ, current installation guide, changelog comparisons and
release description together. A protected demo or unverified GHCR image route
must stay outside the advertised newcomer journey.

## Version-specific and historical records

- [v0.9.0 release records](0.9.0/): preserved installation, support and rollback
  evidence for that version, not current onboarding.
- [Historical research catalog](../historical/README.md): retained experiments
  and release replay scripts.
- [Public releases](https://github.com/Perlitten/project-brain-public/releases):
  published notes, artifacts, checksums and SBOMs.

Return to [current documentation](../README.md).
