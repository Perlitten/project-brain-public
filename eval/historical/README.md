# Historical evaluation material

This directory preserves research inputs and demonstrations, separated from
current evaluation tooling. None of this material establishes improvements in
coding outcomes, task time, or total token cost.

| Material | Purpose | Reproduction boundary |
| --- | --- | --- |
| [pre-web/](pre-web/) | Frozen golden and extension fixtures before the Next.js migration | Use the source revision recorded in the original corpus; schema-v1 replay still references these paths. |
| [synthetic-retrieval/run_standalone_eval.py](synthetic-retrieval/run_standalone_eval.py) | Constructed before/after rankings illustrating a historical RRF tie-breaker issue | No retrieval system is exercised. Outputs explicitly identify themselves as synthetic and ineligible for product claims. |

The synthetic demonstration moved from `eval/run_standalone_eval.py`. No runtime,
test, CI, release or deployment code referenced the original entry point. The
real retrieval regression tests remain in
[`tests/test_retrieval_fusion_remediation.py`](../../tests/test_retrieval_fusion_remediation.py).

Other historical frameworks retain their import paths because tests and replay
scripts still use them:

- [Utility pilot](../../benchmarks/utility_pilot/README.md): version-pinned
  experiments and shared evaluator regressions.
- [Historical release tooling](../../scripts/HISTORICAL.md): version-specific
  contract demonstrations and release evidence generators.
- [Historical trajectory fixture](../../brain/improvement/replay/historical_fixture.py):
  the old `IsolatedReplayRunner` import is preserved by a compatibility module.
  Calling it emits a warning: it executes no code and returns synthetic fixed
  success, token and time values. It is not a production replay capability.

For current evaluation tooling and evidence limits, see [the evaluation guide](../README.md).
