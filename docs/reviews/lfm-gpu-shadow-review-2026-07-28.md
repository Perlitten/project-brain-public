# LFM GPU shadow implementation review — 2026-07-28

## Outcome

The implementation on `feat/lfm-gpu-integration` is approved for deployment to
a private GPU shadow environment, not for production ranking canary.

Final independent review was run through Claude Code `2.1.220` with exact model
`claude-opus-5`, maximum effort and read-only tools. Re-review scope was
`937a15f..1735aad`.

Verdict:

> NO BLOCKERS — SHIP TO GPU SHADOW (not production canary)

## Closed findings

1. Incomplete capture envelopes and partial FastPLAID evidence now fail closed.
2. Shadow mode preserves the authoritative dense baseline element-for-element;
   the wide LFM candidate query is separate.
3. HTTP retries can deduplicate shadow writes through a bounded, hashed
   `X-Request-ID` or `X-Correlation-ID`.
4. The remote GPU path emits provider and rerank request, failure, timeout,
   application and latency metrics.
5. Search releases its database session before vector, remote rerank and shadow
   persistence work.
6. Recall@50 is available only with validated depth-50 evidence; the live
   capture requests 50 unique results and records explicit depth status.

## Verification

- Full test suite: `441 passed, 3 skipped`.
- Focused post-fix suite: `58 passed`.
- Ruff, compileall, `git diff --check`: passed.
- Focused mypy over the six critical changed modules: passed.
- GPU Docker Compose configuration: passed.
- Production flags remain off; NVIDIA dense recall remains authoritative and
  fail-open.

## Additional CLI QA

- Antigravity CLI `agy 1.1.8`, Gemini 3.1 Pro High: `APPROVE` for API, MCP,
  context, health/status and CLI surfaces; no High/Medium findings.
- Kiro CLI `2.15.1`: real read-only inspection started and read the core diff,
  but timed out before a verdict.
- Devin CLI `3000.1.27`: correct read-only invocation timed out without output.
- Grok CLI `0.2.101`, Grok 4.5: blocked by account quota (`402`, then `429`);
  no verdict was attributed to it.

## Remaining rollout gates

Deploy the pinned BF16 service to a private NVIDIA GPU host, acknowledge the
model licence, run full sync without prune, verify runtime parity, collect at
least seven days and 500 eligible shadow requests, then run the complete
50-query paired benchmark including the hard RU/RU-to-EN slice. Only a passing
artifact permits a 1% ranking canary.
