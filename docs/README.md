# Project Brain documentation

Start with a working instance, then inspect how its evidence and access controls
fit your workflow. Current guides below describe the maintained source product.
Release-specific and experimental records are separated at the end.

## Start here

| Guide | Read it to… |
|---|---|
| [Getting started](GETTING_STARTED.md) | Install, index a repository and complete your first MCP task |
| [Architecture](ARCHITECTURE.md) | Understand components, source integrity and trust boundaries |
| [MCP](MCP.md) | Configure clients, inspect tools and understand protocol behavior |
| [Dashboard](DASHBOARD.md) | Inspect projects, code maps, memory and service state |
| [FAQ](FAQ.md) | Understand privacy, model costs, maturity and evidence limits |

## Configure and operate

- [Model providers](MODEL_PROVIDERS.md): mock smoke tests, local models and cloud endpoints.
- [CLI reference](CLI.md): indexing, context, health, backups and evaluation commands.
- [Deployment](operations/DEPLOYMENT.md): authenticated remote installation, TLS and scoped keys.
- [Team mode walkthrough](TEAM.md) (experimental preview, `main` only): projects, invitations,
  member sign-in, MCP and revocation, with known limitations; reference:
  [inviting team members](operations/TEAM_INVITATIONS.md),
  [bounded team execution](operations/TEAM_EXECUTION.md) (off by default).
- [Agent deployment](operations/AGENT_DEPLOYMENT.md): operational gates for an agent running deployment.
- [Operator runbook](../deploy/RUNBOOK.md) and [backup / restore](runbooks/backup-restore.md).
- [Example client configs](../examples/) and [reindex-on-push helper](examples/reindex-on-push.yml).
- [Demo policy](DEMO.md): isolated synthetic showcases and anonymous verification.

## Understand memory and evidence

- [Memory semantics](MEMORY.md): layers, consolidation and retrieval.
- [Lesson protocol](MEMORY_LESSON_PROTOCOL.md): evidence and promotion approval rules.
- [Evaluation](EVAL.md): functional gates, benchmark scope and limits.
- [Architecture decision records](adr/): recorded design decisions and their status.

## Contribute

[Contribution guide](../.github/CONTRIBUTING.md) · [Good first issues](good-first-issues.md) ·
[Roadmap](ROADMAP.md) · [Brand system](BRAND.md) · [Security](../SECURITY.md) ·
[Changelog](../CHANGELOG.md) · [License](../LICENSE)

## Release and historical records

[Release documentation](release/README.md) contains the current release
verification procedure and links to version-specific records.
[Historical research and experiments](historical/README.md) is a catalog of
retained designs, evaluation evidence and old workflows. Historical pages are
not current installation instructions or proof of present performance.

Return to the [product overview](../README.md).
