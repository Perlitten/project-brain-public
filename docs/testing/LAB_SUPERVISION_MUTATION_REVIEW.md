# Laboratory supervision mutation control

The existing timeout test can kill its own pytest/controller process when the
mutated runner stops creating a separate child session. In an owned Linux
process session, the original implementation passes and candidate
`brain.lab.engine.xǁValidationRunnerǁ_run_one__mutmut_94` terminates pytest with
SIGTERM (`-15`). This matches, but does not establish, the cause of the separate
hosted shard's exit 143; its raw log is still unavailable.

The same timeout and cancellation cases now exercise the public `run_profile`
API in an owned subprocess. They require a normal report, failed validation,
nonzero command exit and the correct distinct timeout/cancellation state. The
runner, process creation and termination are real. No private Brain method is
patched or inspected. Test count and production source are unchanged.

| Controlled experiment | Original implementation | Lost-session candidate |
| --- | --- | --- |
| Previous timeout case | 1 passed | pytest killed by SIGTERM; inconclusive |
| Improved timeout and cancellation cases | 2 passed | 2 assertion failures, pytest exit 1 |

The configured before/after experiment took 18.84 seconds. This is one
controller-resilience experiment, not a whole-module mutation score or an
additional project-wide kill. Its source hashes, candidate diff and timings are
in [the evidence file](evidence/project-lab-process-supervision.json). Windows
process-tree behavior is not established by this Linux experiment.

## Verification and reproduction

Full offline suite: **2,542 passed, 11 skipped**, 95.73 pytest seconds (98.76
wrapper seconds). Ruff passes; the unchanged mypy gate reports zero errors in
434 files. Existing skips and learned-model limits remain as documented in the
campaign PR #108. Production source and all retrieval inputs are unchanged;
this patch changes no benchmark, release, archive or tag.

Ordinary reproduction after the documented test-environment setup:

```sh
pytest tests/test_change_laboratory.py -q
ruff check .
bash scripts/mypy_gate.sh
```

Mutation reproduction uses PR #108's frozen generated sandbox, offline support
and mutmut configuration. Set `MUTANT_UNDER_TEST` to the candidate above for the
two named cases. **Launch pytest via `subprocess.run(...,
start_new_session=True)` from an outside controller on POSIX**, with a bounded
timeout. Running the old case directly in your shell's process group can kill
the shell/controller. Restore sandbox source and test bytes afterwards. This
experiment neither certifies all 1,456 laboratory candidates nor changes how
other crashes/timeouts are scored.
