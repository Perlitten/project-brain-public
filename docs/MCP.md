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

### Remote transports

The HTTP-backed stdio bridge runs `apps.mcp_server.remote_server` with
`BRAIN_API_URL`, `BRAIN_API_KEY` and `BRAIN_REPO` configured. It needs no local
datastores. Prefer a revocable scoped `pbk_` credential to a shared operator key.
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

Local stdio exposes 13 tools. The HTTP-backed server adds the fourteenth,
`get_diff_review`, for its asynchronous review flow.

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

Memory writes follow the [lesson and approval policy](MEMORY_LESSON_PROTOCOL.md).
A tool's reported evidence is not proof that Brain independently executed tests.


## Protocol features

Besides tools, both servers implement:

* **Tool annotations.** Every tool carries a title and read-only, destructive,
  idempotent and open-world hints, so clients can auto-approve the read-only ones.
* **Resources.** `brain://rules` and `brain://decisions` list the active rules and
  decisions; `brain://payload/{token}` holds the tail of oversized tool results.
* **Prompts.** `plan_change`, `review_changes` and `capture_lesson` encode the
  intended tool sequence for common jobs.
* **Elicitation.** With `BRAIN_MCP_CONFIRM_WRITES=true`, `record_decision` and
  `record_rule` ask the human to approve the write when the client supports
  elicitation. It is opt-in, because an unattended agent would stall on the prompt.
  An enabled legacy prompt fails closed on errors, invalid responses or a
  30-second timeout. Modern clients receive an input request before the write
  retry. Clients without elicitation retain the existing unattended policy;
  confirmation does not authenticate a human independently of the client.
* **Sampling.** When the server-side model fails, the remote `ask_project` asks the
  client's own model to answer from the Brain's context pack instead of returning
  an error.

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
