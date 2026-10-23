# Dashboard polish — validation and limits

Product PRs: [A #185](https://github.com/Perlitten/project-brain-public/pull/185), [B #186](https://github.com/Perlitten/project-brain-public/pull/186), [C #187](https://github.com/Perlitten/project-brain-public/pull/187). Merge order A → B → C, after required CI and review. No server, Vercel settings, release assets or benchmark baselines were changed.

| Verification | Observed result |
| --- | --- |
| Combined Python suite, content head `70577b4` | 3,326 passed, 59 skipped, 324.66 seconds; coverage 80% |
| Follow-up settings/content/upgrade tests after final restart-copy correction | 31 passed, 9.20 seconds |
| Ruff | clean |
| mypy | 0 errors, 486 source files |
| Web regression suite | 128 passed; production build and 4 production integration checks passed (web code unchanged by later Python fixes) |
| Connected local Chromium checks | 9 capability/access/registration assertions and 9 content assertions passed against a synthetic loopback API |
| PostgreSQL upgrade drill | Legacy rows preserved; optional history columns NULL; repeated migration passed in a uniquely created and removed local disposable database |
| Legacy Lite/SQLite upgrade | Two fresh public-startup processes preserve findings and telemetry; new request writes remain available |
| Cross-process query privacy | Already-running MCP with a stale true startup flag stops capture after API disable; restart with that stale flag cannot reactivate it; missing policy stores metadata only |
| Retrieval non-regression | Gate recomputation passed on the unchanged archived 31-task artifacts; no new model capture |

Before correction, the new legacy-Lite and cross-process tests both failed: selecting old insights raised a missing-column error, and the already-running recorder retained query text after disable. Both then passed. The suite also found and corrected registration authorization happening after body validation; ordinary admin registration, read-only denial, invalid names and canonical path bounds are covered through HTTP.

In a configured local offline test environment, reproduce with:

```sh
python -m pytest --cov=brain --cov-report=term -q
python -m pytest tests/test_content_history_upgrade.py tests/test_app_settings.py tests/test_dashboard_content_actions.py -q
ruff check .
MYPY_PYTHON=python bash scripts/mypy_gate.sh
npm --prefix apps/web run typecheck
npm --prefix apps/web test
npm --prefix apps/web run build
npm --prefix apps/web run test:production
python scripts/check_public_launch.py
python scripts/check_docs_consistency.py
python -m eval.product_upgrade_gate docs/release/evidence/pr75-hotfix/before-validated.json docs/release/evidence/pr75-hotfix/after-review3.json
```

The Python runs used existing local test services and blank NVIDIA/OpenAI provider keys. No model downloads, remote inference or paid provider calls were used. These results do not imply that skipped native/platform integration checks ran.

## Deployment and evidence limits

[PR A screenshots](pr-a/README.md), [PR B screenshots](pr-b/README.md), [PR C screenshots](pr-c/README.md) contain Vercel before/after captures of public synthetic previews. Local connected-mode captures supplement them; read-only Vercel demos cannot exercise mutation APIs. The private live dashboard responds with HTTP 401 and its preview requires Vercel SSO. Its twelve real project rows and latency remain unverified. The public Contabo API build is now identified separately below.

Step 0 is now resolved for the public Contabo API: it reports `b2e9be03b9b090352395a7abc8bce5d757025dff`, predating #183. See [the captured backend evidence and owner rollout instructions](backend-step0.md). No container inspection or secret sharing is needed to establish that reported build. The conditional aggregated-project endpoint (Dashboard ≤5 calls) has **not** been implemented; update the old backend before evaluating the fix. Streaming headers are verified separately and do not establish a reduction in total API calls.

After merge, the owner must deploy the API/startup migrations for capability discovery, repository registration and content-history actions. Query text stays off unless opted in; historical queries cannot be recovered. Existing administrator keys remain compatible. Rotation does not automatically switch a deployment credential. Source mounts remain an owner/server action.

Finding grouping covers the loaded page. Historical module-only findings cannot open source until explicit repository/file evidence exists. The findings model remains global, and repository-scoped listing still fails closed.

Update on 23 October: required test/web/privacy checks, Lite and both PR CodeQL analyses passed for A/B/C. Final reviews are clean for A and C; the latest B head has been submitted for a fresh review. A/B/C remain unmerged. Native deployment workflows remain pending. Current-main pipeline #29's CodeQL analysis reaches SARIF publication, which fails with HTTP 403 `Rate Limit Exceeded`; this is distinct from the earlier missing-secret error. No gate was bypassed and no CI settings were changed.
