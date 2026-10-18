# MCP Server — client and protocol guide

## Client configuration

Install Brain using [Getting started](GETTING_STARTED.md) first. Local stdio
connects directly to Postgres, Redis and Neo4j and loads configuration from the
Brain installation; those services must be running. Use absolute paths.

```bash
claude mcp add project-brain -- /absolute/path/project-brain/.venv/bin/python -m apps.mcp_server.server
```

Clients that accept JSON configuration can use:

```json
{
  "mcpServers": {
    "project-brain": {
      "command": "/absolute/path/project-brain/.venv/bin/python",
      "args": ["-m", "apps.mcp_server.server"],
      "cwd": "/absolute/path/project-brain"
    }
  }
}
```

Use the same absolute `repo_path` in tool calls that you passed to indexing.
Repository decisions and rules should use that scope; omit it only for
intentionally global knowledge. Availability of tools does not make the client
invoke them automatically; add an appropriate task workflow to its instructions.
The local server acts as the operator and writes decisions and rules directly.
In a team, set `BRAIN_LOCAL_PRINCIPAL_KEY` to a member's `pbk_` key to make its
writes follow [governed memory](MEMORY_REVIEW.md#local-surfaces-stdio-mcp-server-and-brain-cli)
(a proposal for a member, a direct write for a team administrator).

### Remote transports

The HTTP-backed stdio bridge runs `apps.mcp_server.remote_server` with
`BRAIN_API_URL`, `BRAIN_API_KEY` and `BRAIN_REPO` configured. It needs no local
datastores. Prefer a revocable scoped `pbk_` credential to a shared operator key.
In a team, each member uses their own member key and passes the repository path
the server indexed as `repo_path`, not their local checkout path; see the
[team mode walkthrough](TEAM.md).
Tracked `.mcp.json` and `examples/claude-code-stdio.mcp.json` use environment references, never
literal credentials.

The API also serves native Streamable HTTP at `/mcp`:

```bash
claude mcp add --transport http project-brain https://brain.example.com/mcp \
  --header "Authorization: Bearer pbk_..."
```

It accepts a Bearer or `X-API-Key` credential and enforces caller scopes. With
`BRAIN_PUBLIC_URL` or `BRAIN_PUBLIC_HOST` configured, it advertises OAuth 2.1
with PKCE and human consent for clients that need OAuth. The approver must
already hold the requested scopes. See [Deployment](operations/DEPLOYMENT.md) for TLS,
credential creation, public routing and the single-worker session requirement.
`BRAIN_MCP_HTTP_ENABLED=false` disables the native endpoint.

Native HTTP and hosted OAuth remain experimental: synthetic SDK/auth tests do
not establish interoperability with Claude, Cursor, Codex or a hosted connector.
Use the [native deployment checklist](operations/DEPLOYMENT.md#native-mcp-and-oauth)
before exposing either. Local stdio initialization records a self-reported
client label, not an authenticated agent identity; see the
[measurement contract](DASHBOARD.md#request-collection).

## Tool reference

Local stdio exposes 13 tools. The HTTP-backed stdio bridge and native `/mcp`
add `get_diff_review`, for the asynchronous review flow, and the twenty
`team_*` tools of the [experimental team mode](TEAM.md): 34 tools in all. The
task and memory review tools answer only for verified team members within their
project roles; `team_me` works in solo state too; `team_request_run` records a
run. MCP has no tool to execute it; the first, off-by-default execution slice is
HTTP only ([bounded team execution](operations/TEAM_EXECUTION.md)). The five
memory review tools follow [governed memory](MEMORY_REVIEW.md). Of the ten
coordination tools, four read and six write: claim, renew and release a lease,
declare a task's scope, queue a run for integration, and (team administrators
only) force-release another member's lease with a reason. They call the same HTTP
routes, so the same project roles, fencing tokens and plan versions apply.
Creating subtasks and integrating or rejecting queue entries stay on the HTTP API
and the web Board ([team coordination](TEAM.md#team-coordination-m3)).

| Tool | Purpose |
|---|---|
| `search_code` | Retrieve files, symbols and chunks using hybrid search |
| `find_related_files` | Inspect graph neighborhoods |
| `ask_project` | Answer with retrieved context using the configured LLM |
| `prepare_task_context` | Build a scoped context pack for a task |
| `prepare_deep_context` | Queue a quality-first context pack |
| `get_background_job` | Poll queued work and retrieve its result |
| `impact_analysis` | Inspect likely change impact and risks |
| `review_diff` | Check a diff against applicable constraints |
| `get_diff_review` | Remote only: poll an asynchronous diff review |
| `record_decision` | Record a canonical global or repository-scoped decision |
| `record_rule` | Record an explicit global or repository-scoped rule |
| `record_task_observation` | Record unvalidated observations |
| `record_task_lesson` | Record a lesson and reported validation evidence |
| `record_procedure_outcome` | Record a procedure outcome once per task |
| `team_me` | Remote only, experimental: who am I, team state, project roles and key scopes |
| `team_activity` | Remote only, experimental: newest-first team activity the caller may read |
| `team_list_tasks` | Remote only, experimental: list a team project's tasks |
| `team_create_task` | Remote only, experimental: create a task in a team project |
| `team_request_run` | Remote only, experimental: record a run request (not executed) |
| `team_propose_decision` | Remote only, experimental: propose a decision for independent review |
| `team_propose_rule` | Remote only, experimental: propose a rule for independent review |
| `team_list_proposals` | Remote only, experimental: list a review queue or your own proposals |
| `team_approve_proposal` | Remote only, experimental: approve someone else's proposal (Reviewer) |
| `team_reject_proposal` | Remote only, experimental: reject someone else's proposal (Reviewer) |
| `team_task_plan` | Remote only, experimental: a task's subtasks, dependency states and ready set |
| `team_coordination_overlaps` | Remote only, experimental: declared-scope overlaps among active tasks and leases, plus import-dependency warnings when the code graph is current |
| `team_list_leases` | Remote only, experimental: critical leases with holders and fencing tokens |
| `team_integration_queue` | Remote only, experimental: the integration queue and each entry's last check |
| `team_claim_lease` | Remote only, experimental: claim a fenced lease on a path or resource (Contributor) |
| `team_renew_lease` | Remote only, experimental: extend a lease you hold, with its fencing token |
| `team_release_lease` | Remote only, experimental: release a lease you hold, with its fencing token |
| `team_declare_scope` | Remote only, experimental: declare the paths a task touches (plan version compare-and-swap) |
| `team_enqueue_integration` | Remote only, experimental: queue a run that reported its head revision for integration |
| `team_force_release_lease` | Remote only, experimental: release another member's lease with a recorded reason (team administrator) |

Memory writes follow the [lesson and approval policy](MEMORY_LESSON_PROTOCOL.md).
A tool's reported evidence is not proof that Brain independently executed tests.


## Freshness envelope

Every tool that answers from the index — `search_code`, `find_related_files`,
`ask_project`, `prepare_task_context`, `prepare_deep_context`, `impact_analysis`,
`review_diff` and the remote `get_diff_review` — returns a `freshness` block, and
so do the REST endpoints behind them (`/search`, `/related`, `/ask`, `/context`,
`/deep-context`, `/impact`, `/diff-review`). The shape is the same everywhere,
so an agent decides from `state` alone whether to trust the answer, re-check the
source, or wait for the named re-index. Existing fields are unchanged; the block
is added beside them.

| Field | Type | Meaning |
|---|---|---|
| `state` | `fresh` \| `stale_served` \| `stale_blocked` \| `unknown` | Whether the answer reflects current source |
| `reason` | string code | Why the state was chosen (table below) |
| `indexed_commit` | string or null | Commit the index was built from |
| `indexed_at` | ISO-8601 UTC or null | When that index was written |
| `age_s` | integer or null | Seconds since `indexed_at` |
| `reindex_job_id` | string or null | Job queued by the auto-reindex, when one was; poll it with `get_background_job` |

The freshness module's own observations (`index_status`, `source_head_commit`,
`branch`, `commits_behind`, `source_revision_kind`, `source_present`, `note`, and
any origin-tracking fields it adds) are passed through when they carry a value.
The six fields above are always present, `null` when unknown.

| `state` | `reason` | What happened |
|---|---|---|
| `fresh` | `index_matches_source` | Indexed commit equals the source head |
| `stale_served` | `source_ahead_of_index` | Source moved past the index; changed candidates were read from disk |
| `stale_served` | `working_tree_dirty` | Uncommitted edits; the auto-reindex waits for a clean tree |
| `stale_served` | `index_age_exceeded` | The freshness module aged the index out |
| `stale_served` | `source_head_unverified` | No git head to compare against; served as-is |
| `stale_served` | `diff_unverified` | The source diff could not be bounded; served as-is |
| `stale_blocked` | `policy_strict_block` | `BRAIN_STALE_CONTEXT_POLICY=strict_block` refused a stale index |
| `stale_blocked` | `live_source_unavailable` | The source tree is missing, so stale slices cannot be refreshed |
| `unknown` | `repository_not_indexed` | No index for the requested path |
| `unknown` | `repository_not_specified` | The tool was called without a repository |
| `unknown` | `source_missing`, `source_unverifiable`, `unindexed` | The freshness module could not assess the repository |
| `unknown` | `not_reported_by_api` | The remote bridge talked to an API that predates the envelope |
| `unknown` | `tool_failed`, `freshness_unavailable` | The tool or the lookup failed; the answer carries its own `error` |

`stale_blocked` is a structured outcome, not an error: `ask_project` and
`prepare_task_context` return `status` and the envelope so an agent can wait for
`reindex_job_id` instead of parsing prose. `ask_project` returns its answer as
text and repeats it with the envelope in `structuredContent`.

The envelope is a trailer appended after the payload budget is applied, so the
inline byte limits in `BRAIN_MCP_INLINE_MAX_BYTES` and `max_tokens` never drop
it and it is not counted against them. The freshness lookup itself is bounded
to a few seconds; past that the block says `unknown` and the answer goes out.
Operators watch the same ages on `/health` (`freshness.repositories[].age_s`
against `BRAIN_FRESHNESS_MAX_AGE_S`).


## Protocol features

Besides tools, both servers implement:

* **Tool annotations.** Every tool carries a title and read-only, destructive,
  idempotent and open-world hints, so clients can auto-approve the read-only ones.
* **Resources.** `brain://rules` and `brain://decisions` list the active rules and
  decisions (for a team member: global entries plus those of repositories they
  can read); `brain://payload/{token}` holds the tail of oversized tool results.
* **Prompts.** `plan_change`, `review_changes` and `capture_lesson` encode the
  intended tool sequence for common jobs.
* **Elicitation.** With `BRAIN_MCP_CONFIRM_WRITES=true`, `record_decision` and
  `record_rule` ask the human to approve the write when the client supports
  elicitation. It is opt-in, because an unattended agent would stall on the prompt.
  An enabled legacy prompt fails closed on errors, invalid responses or a
  30-second timeout. Modern clients receive an input request before the write
  retry. Clients without elicitation retain the existing unattended policy;
  confirmation does not authenticate a human independently of the client.

## Compatibility only

**MCP sampling — legacy fallback**, not a current feature. If server-side
generation fails, `ask_project` compatibility code may ask a sampling-capable
client to answer from Brain's retrieved context. MCP sampling is deprecated
by [SEP-2577](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2577),
but remains operational in supported versions. Brain's newer-protocol path
uses input requests instead; the SEP made no wire-level protocol change.

Configure a server-side model for normal operation. Client support, approval
and billing vary; cross-client fallback interoperability remains unverified.
The retained compatibility path is not advertised as a supported product feature.

## MCP stdio E2E verification

```bash
python eval/mcp_stdio_e2e.py
# writes reports/mcp-stdio-e2e.json
```

## Suggested agent workflow

Client instructions can request this sequence; exposing MCP tools does not
force an agent to use them:

1. Request `prepare_task_context` before planning a change, using the indexed
   repository's canonical path.
2. Inspect the retrieved constraints and read current source. Context packs
   select evidence; missing files still need investigation when the task requires it.
3. Implement the scoped change and run appropriate tests.
4. Request `review_diff` for recorded-rule checks. This supplements Git and CI;
   it is not proof of correctness.
5. Record decisions or lessons only with accurate evidence, scope and reported
   validation. Follow the memory approval policy.

Return to [Getting started](GETTING_STARTED.md) or the [documentation index](README.md).
