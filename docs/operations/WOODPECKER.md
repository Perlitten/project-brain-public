# Woodpecker migration and cutover

## Current state

The configuration migration is staged and the repository is now activated on
Woodpecker. GitHub Actions have been disabled in repository settings to stop
workflow execution; the YAML files are dormant. This does not establish
successful replacement coverage. The current server has one Linux amd64 Docker
agent with capacity two; Windows and the isolated local-backend capabilities
below are not provisioned. Private S3 artifact credentials are provisioned and the uploader transport has
passed a real upload/download probe. Per-workflow artifact production and release
credentials still need live verification. Keep production publication fail-closed until every required
Woodpecker workflow is green.

Normal verification workflows also accept manual runs on main/master for
bootstrap diagnostics. The release gate deliberately continues to require a
real exact-revision main push, never a manual run.

## Workload inventory

| Existing Actions workload | Woodpecker workflow | Execution requirement |
| --- | --- | --- |
| CI / test, migrations, PostgreSQL race/restore, wheel smoke | `test` | Linux Docker backend; ephemeral pgvector PostgreSQL 15, Redis, Neo4j |
| CI / web, production-mode web tests | `web` | Linux Docker backend, Node 22 |
| CI / Lite Linux | `lite` | Linux Docker backend, Python 3.12 |
| CI / Lite Windows | `lite-windows` | Windows amd64 local backend; Python 3.12, git, PowerShell |
| CI / public privacy and launch checks | `public-privacy` | Linux Docker backend, full git history |
| CI / real Docker execution isolation | `container-real-docker` | Isolated Linux local agent, `capability=brain-docker` |
| Team proxy | `team-proxy` | Same isolated Docker-capable agent |
| Team e2e opt-in | `team-e2e` | Same agent, deployment task `team-e2e` |
| CodeQL Python and JavaScript | `codeql-python`, `codeql-javascript` | Unprivileged Linux Docker backend, pinned locally built CodeQL 2.27.3 image |
| Weekly harness benchmark | `benchmark` | Linux Docker backend; services; cron `benchmark` or deployment task `benchmark` |
| Weekly lock update | `lock-update` | Linux Docker backend; cron `lock-update` or deployment task `lock-update` on main |
| Whole-package mutation baseline | `mutation-baseline` | Linux Docker backend; services; deployment task `mutation-baseline`; 32 matrix shards |
| Tagged release | `release` | Separate Linux local agent, `capability=brain-release`; Python 3.12, Docker, gh, Syft |

Normal CI runs for PRs targeting main/master and pushes to those branches.
Team proxy push/PR path filters match the source GitHub workflow. Mutation
measurement is explicit opt-in through deployment task or a mutation-baseline
label update; external PRs still require server-side approval. Use the server's existing two execution slots as the
concurrency ceiling; each shard has its own database and 335-minute measurement
limit. Set the server workflow timeout to at least 360 minutes for mutation runs.

## Agents and secrets

Keep ordinary Python/Node workflows on the unprivileged Docker backend. The
local backend has no isolation: Docker/Windows agents must be disposable,
restricted to this repository, and must not run on production or alongside
release credentials. Retain approval requirements for external PRs. Provision
reviewed, pinned versions of native tools and record them in the server setup.
Local agents use a credential-free checkout of this public repository; they do
not need a forge token or a clone-plugin executable.

The Docker test agent needs Python 3.12, git, Docker Engine with Compose v2 and
PostgreSQL 15 clients. Limit its concurrency to one because existing team e2e
uses fixed container names. The release agent also needs `gh`, `syft` and a
separate Docker configuration directory with no pre-existing credentials.
CodeQL now runs inside the ordinary unprivileged Docker sandbox with the official
2.27.3 bundle and matching standard query packs, SHA-256 verified during image build.
It needs no host Docker socket, privileged mode or new local agent.
CodeQL runs outside Actions; successful main-push SARIF is uploaded to GitHub code
scanning. PR analysis is stored as SARIF; automatic GitHub PR alert comparison
still needs a credential-isolated uploader before the old CodeQL workflow is removed.

Configure these Woodpecker repository secrets; do not copy them into source:

| Secret | Scope |
| --- | --- |
| `github_release_token` | Tag event, contents write and GHCR package write, release agent only |
| `woodpecker_server` | HTTPS origin of the existing server |
| `woodpecker_repo_id` | Numeric Woodpecker repository ID, not the GitHub ID |
| `woodpecker_read_token` | Read pipeline results; tag event only |
| `github_lock_update_token` | Main cron/deployment only; contents and PR write |
| `github_code_scanning_token` | Main push only; security events write |
| `ci_artifact_bucket`, `ci_artifact_endpoint` | Private S3-compatible artifact destination |
| `ci_artifact_access_key`, `ci_artifact_secret_key` | Write-only repository prefix; restrict secret access to the approved S3 plugin image/binary |

Provision the S3 plugin binary `plugin-s3` on local agents. Docker agents use the
published Woodpecker S3 plugin image. Artifacts are separated by pipeline,
workflow and mutation shard. Keep benchmark reports for 90 days; mutation raw
metadata/witnesses for at least one day. Configure these lifecycle policies on
the artifact store. Mutation metadata is retained in artifacts; automatic cache
restore/resume remains a cutover prerequisite, not a claim of this staging PR.

Create server cron entries on main: `benchmark` and `lock-update` at
`0 6 * * 1`, and `codeql` at `17 4 * * 1` (UTC). Cron schedules are server
configuration; a `cron` filter in YAML does not register a schedule.

## Production and release gate

The release gate supports an explicit `BRAIN_CI_PROVIDER=woodpecker` provider.
It retrieves paginated pipeline history, chooses the latest exact-SHA main-push
pipeline, and requires every named workflow to have succeeded. Skipped/missing
Windows or Docker results fail closed. The legacy GitHub provider remains the
default during staging.

For production, put `BRAIN_WOODPECKER_SERVER`, `BRAIN_WOODPECKER_REPO_ID` and
`BRAIN_CI_PROVIDER=woodpecker` in `.env` only at cutover. Put the read token
`BRAIN_WOODPECKER_TOKEN` in the host-only mode-0600 `.deploy.env`. It is never
passed into application containers. Production checks CI before backups/builds.

The release pipeline checks the immutable pushed tag and package version,
builds distributions once, validates installed-image SBOM coverage and image
revision labels, verifies checksums, then rechecks CI and refuses replacement of
an existing release. Publication uses those exact built files and image bytes.
A release runner must have only one slot. Do not trigger a real tag just to test
this migration: validate build/checksum/SBOM steps without publishing first.

## Cutover acceptance

1. Activate the repository using config path `.woodpecker/`, approve runner
   restrictions, provision the missing Windows/Docker/release capabilities,
   scoped secrets and artifact store, and register the three crons.
2. Run the migration PR on Woodpecker. Check every required status, database
   readiness, Windows behavior and artifact retrieval. Validate opt-in benchmark,
   e2e, one mutation shard plus resumability, and lock PR creation. Complete the
   isolated PR SARIF uploader and mutation cache restore mentioned above.
3. Validate release builds/SBOM/identity/checksums without pushing images or
   creating a release. Run the gate against a green and a red exact-revision
   main-push fixture. Configure production's Woodpecker connection and validate
   its read access without deploying the application.
4. Replace the required Actions checks in GitHub branch protection/rulesets with
   the exact observed Woodpecker contexts. Keep all nine required workflows in
   `scripts/woodpecker_gate.py`; do not remove Windows/Docker checks to force a
   green pipeline.
5. After a verified green main push, remove all eight `.github/workflows/*.yml`,
   remove the `github-actions` Dependabot section, switch the production provider,
   and update existing workflow-contract tests and links to the new definitions.
   Confirm that no automatic Actions run is created on a subsequent push.

## Local validation performed

Woodpecker CLI 3.19.0 strict lint accepts all 14 workflow files. Bash syntax,
Python compilation, repository Ruff checks and 86 focused gate/release/deployment
and inventory tests passed (one existing skip). These checks do not claim that
any Woodpecker agent, secret, cron, artifact upload or publishing run has passed.

## Artifact storage on Contabo — 2026-10-20

Private repository-specific Garage buckets use path-style S3. The benchmark prefix
`benchmark/` retains artifacts for 90 days, mutation prefix `mutation-baseline/`
for one day, and test prefix `test/` for seven days. Older `project-brain/` objects
retain the previous seven-day policy. No artifact is public. The current two-slot
Linux agent is not a replacement for the isolated local Docker/release or
Windows agents listed above; those capabilities remain unfinished.

The first live privacy gate rejected shallow history. Clone steps explicitly set
`partial: false` alongside `depth: 0`; zero depth alone did not request full history
from plugin-git. The privacy check remains fail-closed and must pass on a real clone.

## Containerized CodeQL toolchain — 2026-10-21

On this single Docker host the administrator builds the image before enabling the
two CodeQL workflows; it is not built from PR code and no socket is mounted into CI:

```
docker build -t localhost/woodpecker-codeql:2.27.3-5e7607e1b721 -f scripts/ci/images/codeql.Dockerfile scripts/ci/images
```

The Dockerfile pins the Python base digest and official CodeQL bundle checksum.
The workflows pin the installed local manifest
55926369f574f467df1435391e5995addf658bda06ff1eb9c5722676cef54209; each step
checks CLI version 2.27.3. The digest was verified by an actual pull=never
container run on Contabo. Rebuilding after package changes requires reviewing and
updating the image digest in both workflows; a mutable tag is not used by CI. The localhost image name fails closed if the
image is absent; no public-registry fallback is used. Build the same reviewed
image on any additional Docker host before scheduling these jobs there.
The original build-mode=none analysis script, two threads, 4096 MiB query RAM,
exact-SHA anonymous public checkout and main-only publication command are retained.
SARIF archives use the plugin-restricted private S3 image and codeql/ seven-day
lifecycle. Manual diagnostic variable PIPELINE_TASK=codeql selects both languages
on a review branch without credentials or publishing. This is not a main-push
release attestation. Runtime validation is pending at this source checkpoint.
GitHub SARIF publication still needs the separately scoped credential/uploader;
its absence must not be converted into an omitted release gate.
