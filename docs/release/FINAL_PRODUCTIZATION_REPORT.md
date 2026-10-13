# Final productization report

Verification date: 2026-10-13. Published compatible patch: **v0.9.2**.
Base: `1a2adbc8540548281279ebbbf8a8eda131b0cb8a`.
This record reports completed publication and acceptance checks. The source
archive retains its dated release-preparation snapshot; this current report adds
the subsequent anonymous download and installed-distribution evidence.

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
| Tracked source tree | 932 files | 949 files: focused guides, safety helpers, regressions and review evidence added |
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
| Published distributions | Archive checks, clean wheel/sdist installs, CLI/API version and real MCP passed |
| GitHub CI / CodeQL | Green on the runtime merge and exact reviewed release-tag commit |
| Release pipeline | All six jobs passed: distributions, three images, SBOM and publication |

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
`7043828560` points to merged runtime SHA
`566d985464bb6200b67779eaf3d431d0129d4eb2`. The post-merge deployment was
rechecked on desktop/mobile with the new global synthetic/read-only notice.
The protected operator dashboard still returns 401.
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
after all CI/CodeQL and deployment checks passed. The separately reviewed
release-marker commit passed CI and CodeQL before tagging.

The actual public `v0.9.2` checkout was subsequently verified at
`ceffe3a2d5ad6b4fa330b2d972442bd58c6f7a49`: clean tree, doctor, successful
873-file reindex, exact indexed tag SHA and real MCP search/context.
Downloaded wheel and source archive were installed into separate fresh virtual
environments. Both exposed version 0.9.2, CLI/API modules and 13 MCP tools; both
returned the expected source and five-file task context while running outside
the checkout with source imports excluded. The mock critic reported
`PARTIAL_CONTEXT`; these remain functional smoke checks.

Repository metadata writes from integration tokens return `Resource not accessible by
integration`. Content, pull-request and release permissions are available;
those permissions do not confer Vercel or repository-settings administration.
The repository owner subsequently configured the public homepage
(`https://project-brain-demo.vercel.app/`) and discovery topics (`ai-agents`,
`code-search`, `developer-tools`, `mcp`, `project-memory`, `self-hosted`).

## Published release and artifacts

Release: [v0.9.2](https://github.com/Perlitten/project-brain-public/releases/tag/v0.9.2),
published 2026-10-13 at 16:31:51 UTC. Immutable source commit:
`ceffe3a2d5ad6b4fa330b2d972442bd58c6f7a49`.
[Release pipeline](https://github.com/Perlitten/project-brain-public/actions/runs/38430923075)
and all prerequisite CI/CodeQL checks passed. Existing v0.9.1 remains untouched.

Wheel, source archive, SPDX SBOM and `SHA256SUMS` were downloaded without target
authentication, with curl configuration disabled and TLS verification enabled.
Every listed SHA-256 matched; the SBOM parsed successfully. The 834,493-byte
wheel contains Python runtime/resources and no frontend tree or Python caches.
The source archive retains Compose, bootstrap, frontend source/lockfile and
public environment templates. Source archives carry no Git revision metadata.

| Asset | Verified SHA-256 |
|---|---|
| `project_brain-0.9.2-py3-none-any.whl` | `14384aec569b8caafe11236458d4636393aa3ebc32a4bda918dd9705200df7c9` |
| `project_brain-0.9.2.tar.gz` | `1c954fecc0d543dccea8f8a7c58601f5395b64c261fc6a31303e59142e701783` |
| `sbom.spdx.json` | `a91dca5db9b7d265634e8138048ea0b1e6f042e8ab840f9c4056b8d7df39e7db` |

Compared with the prior local v0.9.1 gate, Python regressions increased from
1,965 to 1,976 passing tests, web unit tests from 36 to 39, and production
integration cases from one to two. Optional skips remained 11. These are
coverage-of-behavior counts, not performance or effectiveness measurements.

## External visitor acceptance

| Visitor | Demonstrated result / remaining friction |
|---|---|
| New developer | Tagged public source, bootstrap, index, actual start/restart, clean reinstall and first MCP task passed; cloud cache/plugin adaptations are documented above |
| AI infrastructure engineer | Current architecture documents storage/MCP/model trust boundaries; migrations, scoped source references and distribution checks passed; mocks and partial context limit retrieval conclusions |
| Technical hiring manager | Concise landing page, genuine labeled desktop/mobile visuals, public demo, configured homepage/topics and reproducible release evidence |

## Outstanding limitation and assessment

**GO.** Runtime cleanup, documentation, interface fixes, public demo,
CI/CodeQL, the verified public release and repository homepage/topic metadata
are complete (`homepageUrl` set to `https://project-brain-demo.vercel.app/` and
topics `ai-agents`, `code-search`, `developer-tools`, `mcp`, `project-memory`,
`self-hosted` configured by the repository owner):

```bash
gh repo edit Perlitten/project-brain-public --homepage https://project-brain-demo.vercel.app/ --add-topic mcp,ai-agents,self-hosted,developer-tools,project-memory,code-search
```

No account-wide Vercel configuration or security-alert inventory was certified.
Current tested runtime isolation, operator protection and passing CodeQL are
separate evidence. macOS/WSL2/native Windows, native-agent automatic tool use,
public-container installation and real-model task outcomes remain outside the
verified scope. Coding correctness, completion, time savings and total token
cost benefits require controlled task-level measurement.
