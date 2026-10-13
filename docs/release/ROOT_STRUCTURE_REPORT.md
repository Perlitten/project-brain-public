# Root directory consolidation

Baseline: `f7805e06f7478d3de56a9062d60f9d1ee5d845bd`, after v0.9.2.
The focused metadata/portable-Compose fix is delivered separately in
[PR #63](https://github.com/Perlitten/project-brain-public/pull/63).

Counts describe the tracked GitHub tree. Local `.git`, `.env`, virtual
environments, caches, reports and context packs are excluded and preserved.

## Before and after

**Before: 25 files + 11 directories = 36 root entries.**

```text
.
├── .github/
├── apps/
├── benchmarks/
├── brain/
├── deploy/
├── docs/
├── eval/
├── examples/
├── rules/
├── scripts/
├── tests/
├── .deploy.env.example
├── .dockerignore
├── .env.example
├── .gitattributes
├── .gitignore
├── .mcp.json
├── .mcp.json.example
├── AGENT_DEPLOYMENT.md
├── CHANGELOG.md
├── CLAUDE.md
├── CODE_OF_CONDUCT.md
├── CONTRIBUTING.md
├── DEPLOYMENT.md
├── Dockerfile
├── LICENSE
├── README.md
├── ROADMAP.md
├── SECURITY.md
├── THIRD_PARTY_NOTICES.md
├── docker-compose.prod.yml
├── docker-compose.quickstart.yml
├── docker-compose.yml
├── pyproject.toml
├── requirements-dev.lock
└── requirements.lock
```

**After: 15 files + 11 directories = 26 root entries.**

```text
.
├── .github/
├── apps/
├── benchmarks/
├── brain/
├── deploy/
├── docs/
├── eval/
├── examples/
├── rules/
├── scripts/
├── tests/
├── .dockerignore
├── .env.example
├── .gitattributes
├── .gitignore
├── .mcp.json
├── CHANGELOG.md
├── CLAUDE.md
├── Dockerfile
├── LICENSE
├── README.md
├── SECURITY.md
├── docker-compose.yml
├── pyproject.toml
├── requirements-dev.lock
└── requirements.lock
```

## Decisions for every baseline entry

| Root entry | Decision | Destination or evidence for retaining it |
|---|---|---|
| `.deploy.env.example` | MOVE | `deploy/.deploy.env.example`; deployment-only credential template, separate from runtime `.env` |
| `.dockerignore` | KEEP | Docker build-context convention; restrictive allowlist protects local credentials/caches |
| `.env.example` | KEEP | `scripts/bootstrap.sh` seeds root `.env`; current source setup depends on it |
| `.gitattributes` | KEEP | Git applies repository-wide text/line-ending rules, including shell/Compose portability |
| `.github/` | KEEP | Actions, issue/PR templates, CODEOWNERS and discovered community policies |
| `.gitignore` | KEEP | Repository-wide exclusion of credentials, runtime data and generated outputs |
| `.mcp.json` | KEEP | Intentional Claude Code project discovery; credential values remain environment references |
| `.mcp.json.example` | MERGE | Canonical HTTP-backed stdio template is `examples/claude-code-stdio.mcp.json`; contradictory root-copy instructions removed |
| `AGENT_DEPLOYMENT.md` | MOVE | `docs/operations/AGENT_DEPLOYMENT.md` |
| `CHANGELOG.md` | KEEP | Release workflow extracts notes from this exact path; conventional public release history |
| `CLAUDE.md` | KEEP | Active project-level agent instructions and package/transport navigation |
| `CODE_OF_CONDUCT.md` | MOVE | `.github/CODE_OF_CONDUCT.md`, a supported GitHub discovery location |
| `CONTRIBUTING.md` | MOVE | `.github/CONTRIBUTING.md`, a supported GitHub discovery location |
| `DEPLOYMENT.md` | MOVE | `docs/operations/DEPLOYMENT.md` |
| `Dockerfile` | KEEP | API/worker CI and release build contexts use the repository root and this filename |
| `LICENSE` | KEEP | Top-level licensing convention and packaged MIT notice |
| `README.md` | KEEP | GitHub landing page and Python package long description |
| `ROADMAP.md` | MOVE | `docs/ROADMAP.md` |
| `SECURITY.md` | KEEP | GitHub policy discovery and immutable released wheel metadata link to this root path; moving it would break that public URL |
| `THIRD_PARTY_NOTICES.md` | MOVE | `docs/legal/THIRD_PARTY_NOTICES.md`; license texts and asset paths retained |
| `apps/` | KEEP | Established CLI/API/MCP/web interfaces, imports and Docker/package contracts |
| `benchmarks/` | KEEP | Maintained utility-pilot framework and historical reproducibility; no corpus relocation |
| `brain/` | KEEP | Established core Python package and runtime imports |
| `deploy/` | KEEP | Infrastructure, scripts, production Compose and clearly experimental image topology |
| `docker-compose.prod.yml` | MOVE | `deploy/docker-compose.prod.yml`; project-directory anchoring preserves root contexts/env/mounts |
| `docker-compose.quickstart.yml` | MOVE | `deploy/experimental/docker-compose.quickstart.yml`; remains outside supported newcomer onboarding |
| `docker-compose.yml` | KEEP | Default developer Compose discovery, bootstrap/start commands and root build context |
| `docs/` | KEEP | Current guides, legal/operational material and separated historical records |
| `eval/` | KEEP | Current evaluation tooling and frozen historical targets; module/path contracts retained |
| `examples/` | KEEP | Small, discoverable MCP client configurations, separate from production credentials |
| `pyproject.toml` | KEEP | Required packaging/editable install metadata and lint/type configuration |
| `requirements-dev.lock` | KEEP | Hash-locked CI/developer dependencies referenced by workflows |
| `requirements.lock` | KEEP | Hash-locked runtime and clean-wheel install contract |
| `rules/` | KEEP | Golden task/evaluation contract plus packaged rules; imported and included in build manifests |
| `scripts/` | KEEP | Development, indexing, evaluation and release utilities with documented entry points |
| `tests/` | KEEP | Regression/integration suites, CI and deployed source-retrieval evidence |

Nine files moved with `git mv`; one redundant root template was merged.
No root forwarding stubs, runtime package moves or benchmark-data edits were
introduced. Directory count stays constant because all eleven areas have active
roles; cosmetic consolidation would break useful interfaces or reproducibility.

## Reference and build-path repairs

- Production scripts and direct documented commands use
  `docker compose --project-directory . -f deploy/docker-compose.prod.yml`
  from the repository root. The `.env`, Docker context, persistent bind mounts,
  default relative repository mounts and root `docker-compose.local.yml`
  overrides resolve exactly as before.
- Docker's restrictive context allowlist includes the moved production file.
  The builder copies it to `/release-inputs/deploy/`; the host deployment
  manifest includes `deploy/docker-compose.prod.yml` at the identical depth.
  A digest regression now derives actual COPY destinations from the Dockerfile.
- Experimental Compose retains required credentials, explicit image tags,
  loopback ports and health dependencies. The portable empty-env test helper
  from PR #63 remains intact; only its manifest path changes.
- Root README, current onboarding, contribution/security/legal links, deployment
  commands, CI documentation guards and deployment tests use the new locations.
  The historical installation document receives only a navigation-link repair;
  versioned instructions, frozen benchmark records and published archives stay
  unchanged. The old inventory is explicitly a dated snapshot, not current paths.
- MCP discovery stays at root. The canonical remote template retains environment
  references, and the local desktop example names the existing `.server` module
  instead of a package without `__main__`. Neither example promises native
  Windows installation support.

## Verification and delivery

Targeted link, Compose, deployment-digest and MCP configuration checks: **46
passed**, with Ruff and shell syntax checks passed. Full quality gates, isolated
bootstrap/index/MCP acceptance, packaging, production preflight and remote
CI/CodeQL are pending in this preparation record. This section will be replaced
with observed results before final delivery.

No new release is planned for this source-layout-only change. The published
v0.9.0/v0.9.1/v0.9.2 tags and release assets retain their original layouts and
contents. Current operational guides explicitly distinguish the current layout
from a v0.9.2 checkout. Runtime API/CLI/MCP behavior and package version remain
unchanged.
