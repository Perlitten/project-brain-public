# Owner dashboard and measurement contract

The daily navigation is Dashboard, Projects, Memory, Quality, Activity and Settings.
Existing routes remain valid. Project tools are secondary navigation; global reports,
settings and findings keep project context for the return trip but explicitly show
service-wide evidence. Reranker diagnostics query the selected project.

## Dashboard

The landing page separates latest index coverage from requests in 24h/7d/30d UTC
windows and historical evaluations. Coverage below 100% cannot round up to 100%.
Unknown repositories fail closed. All-project coverage is unknown when any included
project has no coverage observations. A current source revision is not retrieval
correctness. The memory-map animation is a visualization of a snapshot, not live
agent activity or a MYCELIUM organ event.

## Request collection

Durable PostgreSQL tables `brain_request_telemetry` and `brain_telemetry_state` are
created by the existing startup migration. The singleton start timestamp survives
API restarts. Measured sources are HTTP `/search`, HTTP `/context`, and direct MCP
`search_code` / `prepare_task_context`. Other tools, `/ask`, async deep-context jobs
and external agent outcomes are not included. Counts represent attempts, including
retries; UUIDs correlate attempts and do not establish retry deduplication.

Stored fields: operation, HTTP/MCP surface, repository attribution, identity label,
request ID, outcome, recorded timestamp and handler duration. Prompts, queries,
response content, API keys and code excerpts are not stored. MCP currently uses
an unattributed identity; it does not inflate the count of known identities.

Duration measures handler/tool execution before telemetry persistence. HTTP auth,
validation, serialization, network and frontend rendering are excluded. Latency
includes observed successful, partial, empty and failed calls; p50/p95 use
nearest-rank percentiles. This is server handling time, not wire end-to-end latency.

Context delivery means a nonempty technical response without partial/low-confidence
status. It does not establish relevance or task completion. Empty and partial
responses are separate from technical failures. Error/failed/timeout/cancelled
outcomes count as failures. Rejected authentication or schema validation never
enters the instrumented handler and is outside this denominator.

Fixed hourly buckets (24h) or daily buckets (7d/30d) retain time gaps. Buckets before
collection have null counts; zero after collection means no recorded attempts, subject to the skipped-write limitation below.
Partially observed edge buckets are marked. No samples means null latency, never
zero milliseconds. An unavailable table/state returns unavailable metrics, never
demo values. Dashboard aggregation fails honestly above 100,000 observations in a
window instead of silently sampling totals. A SQL aggregate store is the next step
if measured volume reaches that limit. Telemetry persistence has a 500ms budget and
cannot replace the tool result; skipped writes are logged and may leave gaps.

## Quality

`/api/web/quality` reads the fixed, versioned `eval/quality/owner-benchmark-20261008.json`
archive. It includes SHA256 source provenance, separately measured search/context
release identities, the golden corpus hash, sample counts and limitations. It is
historical evidence, independently of the dashboard period. It is packaged through
the existing Docker `eval/` copy and source-manifest inputs. Missing or malformed
artifacts return HTTP 503. This initial archive is a snapshot, not an automatically
refreshed benchmark history. Saved reports remain available for further evaluations.

Usefulness and token/time/cost savings remain not measured until comparable task
outcomes with and without Brain are captured. Coverage, saved pack counts and 200
responses do not prove those outcomes.
# Agent connection

Setup presents one selected client configuration at a time. Claude Code, Cursor and other stdio MCP clients keep their existing server-provided launch contracts. JSON is formatted without a shell prompt or truncation; terminal commands are a separate disclosure. Copy uses the complete displayed value and reports clipboard failure. Server paths must be adapted when running the client on another machine.
