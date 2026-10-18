# Architecture and trust boundaries

Project Brain is an early, self-hosted service run by a trusted operator for one
developer or, with the [experimental team mode](TEAM.md) on `main`, a small
team that shares one instance. It combines a code index, dependency graph and
project knowledge, then serves context to coding agents. It does not replace
source inspection, Git or CI.

The [single-team collaboration contract](adr/team-collaboration.md) describes the
target architecture and staged MVP. On `main`, repository-scoped API, MCP and
dashboard requests are authorized per project once a team has members, but team
mode is an unpiloted preview whose bounded execution is off by default; its stage and known gaps are in
[team-mode status](design/team-status.md).

## Runtime components

| Component | Responsibility |
|---|---|
| `brain/` | Indexing, retrieval, memory policy, jobs and shared service logic |
| `apps/api/` | Authenticated REST API and native Streamable HTTP MCP |
| `apps/mcp_server/server.py` | Local stdio MCP; direct core/datastore access |
| `apps/mcp_server/remote_server.py` | Stdio bridge to an authenticated remote API |
| `brain/workers/` | Queued jobs and in-worker scheduler |
| `apps/web/` | Next.js operator interface; authenticated server-side API access |
| Postgres + pgvector | Indexed source, vectors, memory, decisions, rules and jobs |
| Neo4j | Repository-scoped code graph and relationships |
| Redis | Queue/cache coordination and worker heartbeat |

Local stdio and CLI operations use core services directly. The remote bridge
and browser dashboard use the API. Background jobs need a running worker;
the API alone does not process them. [ADR 013](adr/013-worker-scheduler.md)
records scheduler ownership.

## Source and knowledge integrity

Git indexing selects tracked eligible files and records a committed revision.
A dirty source tree is refused so source evidence is tied to a commit. Ignored
frontend artifacts are excluded; symlinks and secret/binary exclusions are
handled separately. Reindex after committed changes, and verify retrieved
references against current source before acting.

Code graph identity is repository-scoped, preventing common file/symbol names
from combining unrelated projects. Rules and decisions can be global or scoped;
use the same canonical repository path for indexing and task context.
See [ADR 012](adr/012-repository-scoped-graph-identity.md).

Learned memory carries evidence and reported validation. Eligible L3 clusters
can promote automatically under evidence, severity and contradiction gates;
higher-risk cases and distilled L4 procedures default to approval. Direct
rules, decisions and curated procedures have separate write contracts.
See [memory semantics](MEMORY.md) and the [lesson protocol](MEMORY_LESSON_PROTOCOL.md).

## Trust and data boundaries

1. **Repository to storage.** Indexing copies selected source into databases
   you operate. Apply repository access controls, backups and retention rules
   to both the checkout and stored index.
2. **Storage to model providers.** External embedding models receive chunks.
   External LLMs receive prompts containing selected source, task descriptions
   and project knowledge. Self-hosted storage does not keep those requests local.
3. **Brain to agents.** Tool results enter the client's model/context; its own
   provider, logging and retention policies apply. Repository scoping filters
   retrieval but is not a multi-tenant authorization boundary.
4. **Browser to API.** Next.js reads its API credential on the server. Operator
   authentication and scoped API credentials still need correct configuration;
   deploy behind HTTPS. A local loopback onboarding exception is not a public
   dashboard policy.
5. **Integrations.** Notifications, webhooks and remote services may transmit
   payloads. Review every configured endpoint and integration for private code.

For local model execution, configure both embedding and LLM providers plus
endpoint overrides to local services. Disable unwanted external integrations.
For a remote operator instance, use scoped revocable credentials, protected
storage networks and the [deployment guide](operations/DEPLOYMENT.md).

A public showcase must contain only synthetic data and have no backend
credential or privileged connectivity. It must not share the operator
instance's access exception. See [demo policy](DEMO.md).

## Verification and evidence limits

Unit, integration, migration, production-build, privacy and security checks
verify concrete behaviors. Mock-provider end-to-end tests verify installation
and tool wiring, not semantic relevance. Benchmark protocols and retained
results have their own scope and must not be generalized into claims of
coding improvement or net cost savings; those benefits remain unproven.

Current references: [getting started](GETTING_STARTED.md), [MCP](MCP.md),
[evaluation](EVAL.md), [CLI](CLI.md), [architecture decisions](adr/),
and [historical material](historical/README.md).
