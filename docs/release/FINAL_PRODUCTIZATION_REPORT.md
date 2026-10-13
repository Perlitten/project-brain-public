# Final productization report

Verification date: 2026-10-13. Compatible patch candidate: **v0.9.2**.
Base: `1a2adbc8540548281279ebbbf8a8eda131b0cb8a`.
This preparation snapshot separates completed validation from pending public
publication. The [current verified report](https://github.com/Perlitten/project-brain-public/blob/main/docs/release/FINAL_PRODUCTIZATION_REPORT.md)
records the subsequent publication and download checks.

## Initial state and cleanup

The working v0.9.1 release had a 506-line landing page, stale release references,
historical experiments mixed with current guidance, and frontend/cache files in
Python distributions. The interface also had demonstrable reduced-motion,
mobile labeling, filtering and repository-selection defects.

The [complete inventory](PRODUCTIZATION_INVENTORY.json) classifies all **932
baseline tracked files**: **883 KEEP, 9 REFACTOR, 39 ARCHIVE, 1 REMOVE**.
Classification describes disposition, not a mass deletion plan. Runtime imports,
tests, CI, packaging, public commands and frozen evaluation targets were checked;
lack of a literal reference alone was insufficient evidence of dead code.

| Area | Before | Result |
|---|---|---|
| Public onboarding | Long mixed-purpose README and old release guides | Concise README; focused getting-started, architecture, provider and MCP guides |
| Historical evidence | Synthetic demonstrations alongside current evaluation | Historical catalogs; synthetic fixture explicitly warns that it executes no code |
| Runtime wheel | Next.js development tree and Python caches | Python runtime/resources only; frontend retained in source distribution |
| Product visuals | Old graph asset; mobile/reduced-motion defects | Fresh desktop/code-map/mobile captures; visible labels and keyboard fixes |
| Release verification | Separate partial checks | Six local gates, production integration and source/archive checks |

Retained `benchmarks/utility_pilot/`, frozen datasets, release replay scripts,
older regression suites, migration code and the active `apps/api/helpers.py`
compatibility surface. Added historical catalogs rather than moving files that
public replay commands still reference. Archived the unreferenced standalone
synthetic ranking demonstration under `eval/historical/synthetic-retrieval/`.
Removed only the obsolete graph PNG; its replacement is a genuine current capture.
No benchmark baselines, operator data or existing release tags were modified.

## Engineering and presentation changes

Synthetic list endpoints now filter before pagination. Unknown repositories no
longer display another repository's examples. Reduced-motion panels remain
visible, and the memory visualization avoids unnecessary animation work.
The code map supports keyboard scrolling and gives narrow-screen guidance.
Loading and retry states have clearer accessible status messages.

Every synthetic screen carries a visible read-only example notice, including
mobile. The separate deployment profile uses server-side `BRAIN_DEMO_MODE`:
backend bindings or operator credentials fail closed with 503 before a backend
request. Production integration verifies zero backend calls in mixed-mode
misconfiguration. Existing operator authentication remains intact.

The README is within the requested 150–220-line range. Navigation separates
current instructions, historical research and release records. Automated guards
check current local links/anchors, package/runtime versions, published markers,
tagged clone directories and actual installation/MCP entry points.
The v0.9.1 release description was corrected without replacing artifacts.

Fresh [dashboard](../assets/dashboard.png), [code map](../assets/code-map.png)
and [mobile](../assets/mobile.png) images depict the actual local interface with
synthetic data. They are neither hosted-deployment proof nor benchmark evidence.

## Verification

| Check | Result |
|---|---|
| Full local release procedure | 6 gates passed; 0 failed; 0 skipped gates |
| Python suite | 1,976 passed; 11 skipped; 25 warnings |
| Ruff / mypy | Passed; mypy baseline remains zero |
| Database migrations | Fresh initialization and idempotent reapplication passed |
| Web unit tests | 39 passed |
| TypeScript / production build | Passed |
| Production-mode integration | 2 passed, including fail-closed demo isolation |
| Local browser routes | 22 desktop/mobile cases; no page errors or horizontal overflow |
| Local browser interactions | 8 keyboard, navigation, filtering, repository and state checks passed |
| Candidate distributions | Wheel/source archive checks and clean wheel install passed |

The 11 Python skips concern optional sandbox/native-client/legacy-helper/API-key
fixtures; they are not counted as passed integrations. Full-suite verification
exposed a stale exact-version assertion; it now checks the feature's minimum
release while preserving its behavior regressions.

Linux/Python 3.12 is the verification platform; web CI uses Node.js 22 and the
managed local installation used Node.js 24. macOS, WSL2 and native Windows have
not received equivalent validation. Mock providers establish
functional wiring, not meaningful semantic rankings or coding improvements.
Prebuilt images remain experimental and outside the primary newcomer route.

## Public acceptance and release status

The [public demo](https://project-brain-demo.vercel.app/) passed anonymous
desktop/mobile verification on all ten primary routes: 200 responses, visible
synthetic labels, no observed console/page errors or horizontal overflow, correct
GitHub links and working mobile navigation. The API setup/health bridge returns
503 `not_configured`, and privileged controls are disabled. Deployment
`7043354121` points to `1c537e01e8d61a298ac2a2c17fe94d2056446d6f`.
This verifies the tested runtime; Vercel administration and secrets in other
deployment scopes were not inspected. See [demo acceptance](../DEMO.md).

The fresh public Linux clone passed bootstrap, storage startup, four completed
index/reindex runs, an actual stdio MCP search and task-context call, API/worker
and dashboard health, queued-job execution and a clean dependency reinstall.
The actual `start.sh` ran on its documented ports, stopped live descendants
with Ctrl+C and passed again after restart. Generated paths stayed absent; a
legitimate disposable source function was indexed and retrieved, then removed
and reindexed. Both model roles were mocks; context was marked partial.
An isolated Compose overlay selected independent ports, volumes and credentials
to preserve the existing development services. A cached APOC plugin with a recorded SHA-256
and an owned npm cache accommodated this cloud runtime. No private data or
production configuration was copied into the public source.

Tested source `9da8f12ee16305fa02c3039d7591b0e039572846` has the same tree as
the final reviewed PR head `7b22d620414b7491a8d26f707f8861954bbafae7` and merged
runtime commit `566d985464bb6200b67779eaf3d431d0129d4eb2`.
[PR #61](https://github.com/Perlitten/project-brain-public/pull/61) merged only
after all CI/CodeQL and deployment checks passed. Publication/download
verification for v0.9.2 remains pending in this preparation snapshot.

Repository metadata writes currently return `Resource not accessible by
integration`. Content, pull-request and release permissions are available;
those permissions do not confer Vercel or repository-settings administration.
The protected operator homepage must be replaced with a verified public
destination by a repository owner if that restriction remains.

Planned release: [v0.9.2](https://github.com/Perlitten/project-brain-public/releases/tag/v0.9.2).
This link names the intended publication; the candidate is not yet declared
publicly released or externally accepted. This section will be updated with
actual tag, artifact, source-install and GitHub check evidence after publication.

Current assessment: **NO-GO for declaring the complete productization finished**
until the remaining public acceptance and release gates complete.
Coding correctness, task completion, time savings and total token-cost benefits
remain unproven and require controlled task-level measurement.
