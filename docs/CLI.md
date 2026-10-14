# CLI reference

Installing the wheel or an editable source checkout registers `brain`.
`python -m brain` is the same entry point. The table describes current `main`;
commands marked **main only** are absent from the recommended v0.9.2 release.
See [Getting started](GETTING_STARTED.md) for version-specific installation.

## Discover commands without a provider call

```bash
brain --version
brain --help
brain index --help
brain context --help
brain eval --help
python -m brain.version
```

Use `brain COMMAND --help` and then `brain GROUP SUBCOMMAND --help` for options.
Help/version discovery does not establish a working datastore or provider.

| Command | Purpose / scope |
|---|---|
| `brain doctor` | Validate storage, providers and local installation |
| `brain health` | Inspect configured storage health |
| `brain audit` | Offline repository inventory; writes an audit report |
| `brain index` | Index source and embeddings; requires configured storage/providers |
| `brain context` | Build task context for an indexed repository |
| `brain impact` | Analyze effects of a proposed change |
| `brain review-diff` | Check a Git diff against architecture/test guidance |
| `brain eval` | Evaluate caller-selected golden tasks or validate their YAML |
| `brain graph` | Graph operations; discover subcommands with `--help` |
| `brain decision` | Architectural decision storage |
| `brain rules` | Architecture/compliance rule storage |
| `brain cache` | Stable memory cache inspection/maintenance |
| `brain embeddings` | Embedding status, compatibility verification and backfill |
| `brain import` | Grafify graph / Obsidian vault imports |
| `brain init` | **main only:** initialize a local Lite profile with `--lite` |
| `brain serve` | **main only:** loopback API with a Lite maintenance worker |
| `brain mcp` | **main only:** stdio MCP using the selected profile |

## Audit a distinct checkout

```bash
brain audit --repo /absolute/path/to/target-repository
```

This reads local source without a model call. It writes
`<target-repository>/reports/initial-audit.md`, **not** the Brain checkout's report
directory when these are different repositories. `REPORT_OUTPUT_DIR` explicitly
overrides that directory. The target must be writable for report output.
Without `--repo`, CLI commands use `REPO_ROOT` (default `.`); that fallback is not
the server's `TARGET_REPO_PATH`. Explicit paths must satisfy allowed-root checks.

## Index, context and evaluation

The following commands apply to both v0.9.2 and main. First configure the full
profile and storage, or initialize main's [Lite profile](GETTING_STARTED.md#lite-mode-unreleased-main).
Use one canonical target path; configure real providers for meaningful model
results. Mock/token-hash runs prove plumbing only. Git indexing requires committed
source. Indexing and evaluation can make provider calls with a real configuration.

```bash
brain health
brain doctor
brain index --repo /absolute/path/to/target-repository
brain context "Locate the entry point" --repo /absolute/path/to/target-repository --budget small
brain eval --repo /absolute/path/to/target-repository --golden /absolute/path/to/golden.yaml --validate
```

Removing `--validate` runs evaluation rather than just YAML validation. Use a
golden file designed for **your** target; Brain's `rules/golden_tasks.yaml` describes
the Brain codebase, not arbitrary repositories. See [Evaluation](EVAL.md).
`brain index --clean` deletes that repository's existing index; do not use it as
a casual freshness check. Embedding-model changes need verification/backfill.

## Backups are operator scripts

There is not a `brain backup` command. The source checkout supplies
`scripts/backup_runner.py` and `scripts/restore.sh`; they are separate from the
installed CLI tree. Follow the version-matched [backup/restore runbook](runbooks/backup-restore.md)
with an explicit configuration and disposable rehearsal before touching data.
Restoration is destructive and is not an offline CLI smoke test.

