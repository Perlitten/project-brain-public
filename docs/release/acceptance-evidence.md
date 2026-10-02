# Acceptance evidence — subscriber-ready pass

Author-recorded verification on the integrated stack (#99 → #103) plus the
rendering fixes in #104. Screenshots were first committed in
`222449270bbaf314f7559fb34139e78fc46d689c`; the capture working-tree revision
was not recorded separately. These images show capture-time states and do not
verify later merge-review repairs. GitHub Actions is disabled repo-wide.

## Install / journey

Previously verified end-to-end and recorded in
`docs/launch-readiness/2026-10-02-single-user-journey-validation.md` (install → configure → agent connect →
index → first task → restart → upgrade, ~50s to first task on real services).
Re-verified on this stack: `docker compose up -d postgres redis neo4j` →
`uvicorn apps.api.main:app --port 8010` → `/dashboard/setup` checklist reports
services/repo/provider/index/agent/task as observed — not LEDs.

## Interface states (screenshots, real product, live data)

| Shot | What it proves |
|---|---|
| `screenshots/setup-1440.png` | First-use checklist: 5 done / 1 "action needed" with the concrete hint; repo field + client configs + verify button; 1440px |
| `screenshots/setup-768.png` | Tablet width — same page, same content, no clipping |
| `screenshots/setup-390.png` | Mobile width — checklist table stacks with per-cell labels (STEP/OBSERVED/WHAT TO DO), zero horizontal overflow (scrollWidth 390 = viewport 390) |
| `screenshots/catalog-1440.png` | Executable component catalog — every contract state on production classes |
| `screenshots/catalog-390.png` | Catalog at mobile width |
| `screenshots/overview-1440.png` | Command page with next-action |
| `screenshots/orchestration-1440.png` | Agent runs — completed reindex jobs and queue metadata; the queued-job Cancel action is not exercised by this screenshot |

## Keyboard / focus / motion (measured via Playwright, not eyeballed)

- **Tab order on Get started:** skip-link → rail items in nav order → page
  content. First tab stop is "Skip to content".
- **Focus indicator:** `outline: solid 2px rgb(47,227,200)` on the focused
  element — visible, token-driven.
- **Reduced motion honored:** `prefers-reduced-motion: reduce` collapses
  `pb-shimmer` from 1.4s to effectively 0 (computed `animation-duration` ≈ 0s);
  the busy-button spinner slows to 2.4s instead of removing the state cue.
- **Mobile drawer:** hamburger `data-drawer-open` present and rendered
  (36×36px hit target) at 390px.
- **Contrast:** `#91` verified all token tiers ≥4.5:1; sampled again —
  `--text-3` (#93a1ae) desc/hint text and chip fg/bg pairs remain above that.
  These are DOM-computed styles, not screenshot inference — no WCAG claim is
  made beyond what was directly measured.

## Bugs found by the acceptance pass and fixed in this PR

1. **`pb-field--grow-*` on `__control` stretched fields full-height** inside
   column-flex parents (setup page's repo field rendered ~8 rows tall). The
   grow modifier belongs on `.pb-field` inside horizontal groups; removed
   from the controls in `setup.html` and `catalog.html` — control height is
   now the contractual 36px.
2. **Setup checklist + catalog tables clipped at 390px.** Both tables lacked
   `pb-table__container`, which is what the base.html data-label script and
   the mobile stacked-row CSS key on. Wrapped both — the mobile shot shows
   the stacked rendering.

## Remaining blockers (carried forward honestly)

- Provider-backed legs: real LLM/embedding key unavailable in this
  environment — summary quality, real-provider auth, and agent-level A/B are
  untested (see `docs/release/benefit-verification.md` for measured-with-mock
  results and the still-required legs).
- Unfamiliar-repository corpus for the benefit harness (schema-v2, fixed
  50-task split) — separate deliverable.
- Owner decisions outstanding: LICENSE file choice; repo visibility/release
  publication.
- Remote MCP + n8n legs remain untested on this box (documented in
  `docs/launch-readiness/2026-10-02-single-user-journey-validation.md`).

---

# Correction pass evidence — 2026-10-02 (update)

Verified against master `5cb365c` ("fix(dashboard): anchor Jinja template dir
to package location (#107)"). Changes in this section live on unmerged PRs
where noted; SHAs are exact, not branch tips.

## Reproducible verification

```bash
# in a clone of the integrated commit
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
docker compose up -d postgres redis neo4j
bash scripts/verify_release.sh
```

Result at `5cb365c` on this box: **7 gates passed, 0 failed, 0 skipped**
(ruff, mypy ratchet at 0, design-system guard, deploy stale-scan, live
migrations, full pytest 1574, wheel clean-install). GitHub Actions is
disabled repo-wide — **no remote CI run exists**; `verify_release.sh` mirrors
`.github/workflows/ci.yml` step-for-step, so enabling Actions would run the
identical gates.

## Cold wheel/sdist install (distinct from the ~50s warm journey)

Not the earlier warm-service number — a fresh venv, foreign cwd, wheel built
from the source tree:

- Commit: `5cb365c` (includes the Jinja template-dir fix the cold install
  exposed — dashboard 500'd outside the checkout).
- Fresh venv `/tmp/coldenv`, instance dir `/tmp/brain-cold-run`, cwd outside
  the checkout: `pip install dist/brain-*.whl` → **9.7s** (network-cached
  deps; lock-pinned).
- Exercised on the installed package: `brain doctor` → `.env` config →
  `brain index --repo /tmp/eval-repos/flask` (Flask 3.1.3, pinned
  `22d924701a6ae2e4cd01e9a15bbaf3946094af65`) → context pack → dashboard
  login + CSRF session flow (`brain` user) → MCP stdio handshake
  (initialize + tools/list) → API restart → state persisted.
- Same-version reinstall verified; a true downgrade leg has **no prior
  release to roll back to** — remains untested, not failed.

## Completed vs observed vs untested vs blocked

| Class | Items |
|---|---|
| Completed local checks | verify_release.sh 7/7 at `5cb365c`; cold-install legs above; eval scorer/corpus repair + paired x3 run (PR #106) |
| Observed user outcomes | Setup checklist reports repo-scoped pack completion only after indexing succeeds (PR #105); self-check labeled "MCP server self-check passed"; provider status distinguishes configured/verified/failed/demo |
| Untested legs | Claude Code / Cursor client configs on machines with those clients (marked `client unavailable` in tests); real downgrade; remote MCP; n8n |
| Blocked | Every real-provider leg — no LLM/embedding key in this environment; agent-level with/without-Brain task comparison pending a key via the secret mechanism |

## Open correction PRs (SHAs pinned)

- **#105** `devin/1790948300-readiness-truth` — repo-scoped pack evidence,
  self-check/external-client/provider split, cwd-safe MCP client configs.
- **#106** `devin/1790948600-eval-validity` (`cd022eb`, `b2c20b2`) —
  verbatim-grounded scorer, pinned corpora + holdout, failure attribution,
  rewritten `benefit-verification.md`, committed artifacts in `eval/results/`.

## Owner decisions outstanding (unchanged)

- License + copyright holder — see `docs/release/LICENSE-DECISION.md`
  (not chosen by the agent).
- Repository visibility / public release publication.
