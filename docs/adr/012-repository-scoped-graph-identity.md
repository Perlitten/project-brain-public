# ADR 012: Repository-scoped graph identity and isolation

## Status

Accepted for implementation — 2026-07-27

## Context

The v1 graph merged nodes by `(label, name)`. Repository names were path
basenames, File names were repository-relative paths, and Symbol names were
short identifiers. Relationship endpoints used the same unscoped keys.

Two repositories can therefore share one `README.md`, `main`, or `render` node.
The v1 clean path found a Repository by basename and deleted everything reachable
from it, so a per-repository purge could remove or mutate another repository's
graph.

The Postgres index already owns the canonical repository identifier:
`repositories.id`. Neo4j is a derived read model and can be rebuilt from those
repository-scoped source records.

## Decision

1. Graph schema v2 stores `graph_schema_version=2` and `repository_id` on every
   indexed node and relationship.
2. Every node is merged by:

   ```text
   (label, graph_schema_version, repository_id, identity)
   ```

3. Repository identity is `repository:<repository_id>`.
4. File identity is its normalized repository-relative path.
5. Defined Symbol identity is:

   ```text
   <file_path>::<kind>::<name>::<start_line>
   ```

   Unresolved CALLS/USES references remain explicit file-owned reference nodes;
   they are not silently merged with a definition from another file.

6. Relationship endpoints carry the same `repository_id`. Both endpoints are
   merged inside one GraphClient repository fence.
7. Related-file and impact queries require an indexed repository scope and
   filter every traversed node to that repository and graph generation.
8. Repository purge matches `(graph_schema_version=2, repository_id)` directly.
   It never matches a basename and never discovers ownership through traversal.
9. Composite uniqueness constraints are installed for each allowlisted node
   label.
10. `/context`, `/impact`, `/related`, normative HTTP writers, and their MCP
    equivalents fail closed for missing or unknown repository scope where the
    operation is repository-specific.
11. Graph-assisted retrieval matches Repository nodes by the stable Postgres
    `repository_id`; repository basename is display metadata only. Rerank cache
    namespaces hash the complete stable id rather than truncating its shared
    `repository:` prefix.
12. Re-indexing a changed File replaces that File node, every file-owned Symbol
    (including unresolved references), and their relationships before writing
    the new snapshot.
13. get_focus_neighborhood filters by repository_id and does not cross
    repositories. Existing depth and node bounds keep working.
14. GraphClient is instantiated with repository_id and all graph reads and writes
    are scoped to that repository.

## Consequences

### Positive

- same-named files and symbols in different repositories cannot collide;
- purge ownership is explicit and independent of graph topology;
- graph-derived impact and related results are repository-bound;
- get_focus_neighborhood cannot return cross-project blast-radius answers;
- changed files cannot leave renamed symbols or deleted references behind;
- same-slug normative rules remain independent across repositories.

### Negative

- existing Neo4j data requires validation and potential migration to v2 schema
  before isolation is effective;
- graph results may be unavailable during migration;
- unresolved symbol-reference nodes remain conservative until a separate
  repository-scoped symbol-resolution slice is implemented.

## Verification

- `tests/test_graph_repository_isolation.py` — node and relationship isolation
- `tests/test_graph_cli_scoping.py` — CLI repository resolution
- get_focus_neighborhood is scoped and does not cross repositories
- existing graph data carries repository_id property (or reindex is required)
