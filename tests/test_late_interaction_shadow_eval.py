from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from brain.database.migrations import _ensure_late_interaction_shadow_table
from brain.database.models import LateInteractionShadowEvent
from brain.late_interaction import shadow
from brain.late_interaction.shadow import (
    ShadowEvent,
    hash_query,
    make_shadow_idempotency_key,
    record_shadow_event,
)
from eval import paired_retrieval_eval as paired_eval
from eval.late_interaction_runtime_parity import (
    ParityCorpus,
    ParityDocument,
    ParityQuery,
    evaluate_parity,
)
from eval.paired_retrieval_eval import (
    DEFAULT_BASE_DATASET,
    DEFAULT_EXTENSION_DATASET,
    PRODUCTION_GATE_POLICY,
    evaluate_paired,
    load_dataset,
    load_runs,
    production_gate_thresholds,
    resolve_evaluation_policy,
)
from eval.run_paired_lfm_retrieval import result_record


ROOT = Path(__file__).resolve().parents[1]


def _depth_50_ranking(
    preferred: list[str],
    *,
    first_distractor: bool = False,
) -> list[dict[str, float | str]]:
    paths = list(dict.fromkeys(preferred))
    if first_distractor:
        paths.insert(0, "distractor-first.py")
    index = 0
    while len(paths) < 50:
        candidate = f"depth-evidence/distractor-{index:03d}.py"
        index += 1
        if candidate not in paths:
            paths.append(candidate)
    return [
        {"path": path, "score": float(1000 - rank)}
        for rank, path in enumerate(paths)
    ]


def _variant_with_depth(
    ranking: list[dict[str, float | str]],
    *,
    status: str = "sufficient",
    requested: int = 50,
    corpus_size: int = 100,
) -> dict:
    return {
        "ranking": ranking,
        "depth": {
            "requested": requested,
            "returned_unique": len(ranking),
            "corpus_size": corpus_size,
            "status": status,
        },
    }


def _shadow_payload() -> dict:
    return {
        "repo_id": 7,
        "query_hash": hash_query(7, "where is init_db"),
        "idempotency_key": make_shadow_idempotency_key(7, "request-123"),
        "language": "en",
        "query_class": "exact_identifier",
        "baseline_final_top_k": [
            {"item_id": "file:1", "path": "brain/database/session.py", "rank": 1, "score": 0.8}
        ],
        "counterfactual_final_top_k": [
            {"item_id": "file:2", "path": "brain/database/migrations.py", "rank": 1, "score": 9.2}
        ],
        "score_metrics": {"ndcg_delta": 0.1, "top_k_overlap": 0.5},
        "status": "scored",
        "coverage": 1.0,
        "timing_ms": {"query": 10.0, "maxsim": 20.0},
        "model_revision": "lfm-bf16-a",
        "index_revision": "repo-commit-a",
        "skip_reason": None,
    }


def test_shadow_table_is_query_redacted_indexed_and_bounded():
    table = LateInteractionShadowEvent.__table__
    assert "query" not in table.c
    assert "query_hash" in table.c
    assert "idempotency_key" in table.c
    assert {index.name for index in table.indexes} >= {
        "ix_late_shadow_created",
        "ix_late_shadow_repository_created",
        "ix_late_shadow_status_created",
    }
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_late_shadow_coverage",
        "uq_late_shadow_idempotency_key",
    }
    assert all(
        column.name
        not in {"baseline_final_top_k", "counterfactual_final_top_k", "score_metrics", "timing_ms"}
        for index in table.indexes
        for column in index.columns
    )


def test_shadow_contract_hashes_with_repo_scope_and_forbids_raw_query():
    assert hash_query(1, "same") != hash_query(2, "same")
    assert make_shadow_idempotency_key(1, "request") == make_shadow_idempotency_key(1, "request")
    ShadowEvent.model_validate(_shadow_payload())
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ShadowEvent.model_validate(_shadow_payload() | {"query": "must not persist"})
    with pytest.raises(ValidationError, match="skipped events require"):
        ShadowEvent.model_validate(_shadow_payload() | {"status": "skipped"})
    with pytest.raises(ValidationError, match="at most 50"):
        ShadowEvent.model_validate(
            _shadow_payload()
            | {
                "baseline_final_top_k": [
                    {"item_id": f"file:{rank}", "path": f"{rank}.py", "rank": rank}
                    for rank in range(1, 52)
                ]
            }
        )


@pytest.mark.asyncio
async def test_shadow_record_is_idempotent_statement_and_fail_open(monkeypatch):
    class FakeSession:
        def __init__(self, *, fail: bool = False):
            self.fail = fail
            self.statement = None
            self.committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, statement):
            if self.fail:
                raise RuntimeError("postgres unavailable")
            self.statement = statement
            return SimpleNamespace(rowcount=1)

        async def commit(self):
            self.committed = True

    session = FakeSession()
    monkeypatch.setattr(shadow, "async_session_factory", lambda: session)
    assert await record_shadow_event(_shadow_payload()) is True
    sql = str(session.statement.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in sql
    assert session.committed is True

    failing = FakeSession(fail=True)
    monkeypatch.setattr(shadow, "async_session_factory", lambda: failing)
    assert await record_shadow_event(_shadow_payload()) is False
    assert await record_shadow_event(_shadow_payload() | {"query": "raw"}) is False


@pytest.mark.asyncio
async def test_shadow_retention_cleanup_is_bounded_and_fail_open(monkeypatch):
    class ScalarResult:
        def all(self):
            return [10, 11]

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def scalars(self, _statement):
            return ScalarResult()

        async def execute(self, _statement):
            return SimpleNamespace(rowcount=2)

        async def commit(self):
            return None

    monkeypatch.setattr(shadow, "async_session_factory", FakeSession)
    assert await shadow.cleanup_shadow_events(retention_days=30, repo_id=7, batch_limit=100) == 2
    with pytest.raises(ValueError, match="retention_days"):
        await shadow.cleanup_shadow_events(retention_days=0)


@pytest.mark.asyncio
async def test_shadow_migration_creates_only_scalar_indexes():
    class FakeConnection:
        def __init__(self):
            self.statements: list[str] = []

        async def execute(self, statement):
            self.statements.append(str(statement))

    connection = FakeConnection()
    await _ensure_late_interaction_shadow_table(connection)
    ddl = "\n".join(connection.statements)
    assert "CREATE TABLE IF NOT EXISTS late_interaction_shadow_events" in ddl
    assert "uq_late_shadow_idempotency_key" in ddl
    assert "ix_late_shadow_created" in ddl
    assert "ix_late_shadow_repository_created" in ddl
    assert "ix_late_shadow_status_created" in ddl
    assert "baseline_final_top_k)" not in ddl
    assert "counterfactual_final_top_k)" not in ddl


def test_checked_in_paired_dataset_has_required_size_ru_slice_and_provenance():
    dataset = load_dataset(
        ROOT / "eval" / "golden_set.json",
        ROOT / "eval" / "lfm_eval_extension.json",
    )
    assert len(dataset) == 50
    assert sum(1 for entry in dataset if entry.language.startswith("ru")) >= 15
    assert all(entry.provenance["production_real"] is False for entry in dataset)
    assert len({entry.query_id for entry in dataset}) == 50
    schema = json.loads((ROOT / "eval" / "paired_dataset.schema.json").read_text(encoding="utf-8"))
    assert schema["items"]["required"] >= ["id", "question", "expect_files"]
    assert "provenance" in schema["items"]["required"]


def test_paired_eval_computes_metrics_slices_deltas_and_passes_strong_fixture():
    dataset = load_dataset(
        ROOT / "eval" / "golden_set.json",
        ROOT / "eval" / "lfm_eval_extension.json",
    )
    runs = {}
    for entry in dataset:
        relevant = list(entry.expected_files)
        baseline = _depth_50_ranking(relevant, first_distractor=True)
        lfm = _depth_50_ranking(relevant)
        runs[entry.query_id] = {
            "baseline": baseline,
            "lfm_rerank": lfm,
            "fastplaid": lfm,
        }

    report = evaluate_paired(dataset, runs)
    assert report["aggregate"]["baseline"]["hit@3"] == 1.0
    assert report["aggregate"]["lfm_rerank"]["recall@50"] == 1.0
    assert report["paired"]["lfm_rerank"]["median_deltas"]["ndcg@10"] >= 0.03
    assert report["paired"]["lfm_rerank"]["improved_ndcg_ratio"] == 1.0
    assert "ru-en" in report["slices"]["language"]
    assert "bug" in report["slices"]["query_class"]
    assert report["lfm_rerank_gate"]["passed"] is True
    assert report["fastplaid_gate"]["passed"] is True
    assert report["overall_gate"]["passed"] is True
    assert report["evidence"]["variant_query_counts"] == {
        "baseline": 50,
        "lfm_rerank": 50,
        "fastplaid": 50,
    }
    assert report["evidence"]["recall_at_50_eligible_counts"] == {
        "baseline": 50,
        "lfm_rerank": 50,
        "fastplaid": 50,
    }


def test_paired_eval_rejects_no_improvement_even_without_recall_regression():
    dataset = load_dataset(
        ROOT / "eval" / "golden_set.json",
        ROOT / "eval" / "lfm_eval_extension.json",
    )
    runs = {}
    for entry in dataset:
        ranking = _depth_50_ranking(list(entry.expected_files))
        runs[entry.query_id] = {
            "baseline": ranking,
            "lfm_rerank": list(ranking),
        }

    report = evaluate_paired(dataset, runs)

    assert report["aggregate"]["lfm_rerank"]["recall@50"] == 1.0
    assert report["lfm_rerank_gate"]["checks"]["recall_at_50_non_regression"] is True
    assert report["lfm_rerank_gate"]["checks"]["median_ndcg_delta"] is False
    assert report["lfm_rerank_gate"]["checks"]["improved_query_ratio"] is False
    assert report["lfm_rerank_gate"]["passed"] is False


def test_production_gate_policy_is_fixed_to_baked_datasets_and_thresholds():
    args = SimpleNamespace(
        production_gate=True,
        base_dataset=DEFAULT_BASE_DATASET,
        extension_dataset=DEFAULT_EXTENSION_DATASET,
        median_ndcg_delta_min=None,
        improved_ratio_min=None,
        hit3_regression_max=None,
        slice_regression_max=None,
    )
    assert resolve_evaluation_policy(args) == production_gate_thresholds()

    args.median_ndcg_delta_min = -1.0
    with pytest.raises(ValueError, match="fixed and cannot be overridden"):
        resolve_evaluation_policy(args)

    args.median_ndcg_delta_min = None
    args.base_dataset = ROOT / "eval" / "lfm_eval_extension.json"
    with pytest.raises(ValueError, match="baked default base dataset"):
        resolve_evaluation_policy(args)


def test_production_gate_cli_emits_sha256_provenance_for_all_inputs(
    tmp_path,
    monkeypatch,
):
    dataset = load_dataset(DEFAULT_BASE_DATASET, DEFAULT_EXTENSION_DATASET)
    results = []
    for entry in dataset:
        baseline = _depth_50_ranking(
            list(entry.expected_files),
            first_distractor=True,
        )
        lfm = _depth_50_ranking(list(entry.expected_files))
        results.append(
            {
                "query_id": entry.query_id,
                "variants": {
                    "baseline": _variant_with_depth(baseline),
                    "lfm_rerank": _variant_with_depth(lfm),
                },
                "late_interaction": {
                    "status": "scored",
                    "model_revision": "model-r1",
                    "index_revision": "r1294",
                },
            }
        )
    runs_path = tmp_path / "paired-rankings.json"
    runs_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "repository": {"id": 7},
                "complete": True,
                "failures": [],
                "release_binding": {
                    "repository_id": 7,
                    "build_sha": "build-r1",
                    "source_digest": "d" * 64,
                    "model_revision": "model-r1",
                    "index_revision": "r1294",
                    "lineage_id": "lineage-r1",
                    "identity_digest": "e" * 64,
                    "document_count": 1294,
                },
                "evidence": {
                    "expected_index_revision": "r1294",
                    "expected_lineage_id": "lineage-r1",
                    "observed_lineage_id": "lineage-r1",
                    "expected_identity_digest": "e" * 64,
                    "observed_identity_digest": "e" * 64,
                    "expected_document_count": 1294,
                    "observed_document_count": 1294,
                },
                "results": results,
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "paired-eval.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "paired_retrieval_eval.py",
            "--runs",
            str(runs_path),
            "--json",
            str(report_path),
            "--production-gate",
        ],
    )

    assert paired_eval.main() == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    provenance = report["evaluation_provenance"]
    assert provenance["policy"] == PRODUCTION_GATE_POLICY
    assert provenance["production_gate"] is True
    assert provenance["fixed_thresholds"] is True
    assert provenance["thresholds"] == production_gate_thresholds()
    assert report["release_binding"]["repository_id"] == 7
    artifacts = provenance["artifacts"]
    expected_paths = {
        "runs": runs_path,
        "base_dataset": DEFAULT_BASE_DATASET,
        "extension_dataset": DEFAULT_EXTENSION_DATASET,
        "evaluator": Path(paired_eval.__file__),
        "runner": ROOT / "eval" / "run_paired_lfm_retrieval.py",
    }
    for name, path in expected_paths.items():
        assert artifacts[name]["sha256"] == hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        assert artifacts[name]["bytes"] == path.stat().st_size


def test_production_gate_rejects_legacy_unenveloped_runs(tmp_path):
    path = tmp_path / "legacy-runs.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="schema v2 paired capture"):
        load_runs(path, require_production_evidence=True)


@pytest.mark.parametrize(
    ("complete", "failures", "message"),
    [
        (False, [], "complete must be true"),
        (
            True,
            [{"query_id": "q1", "status": "partial", "reason": "coverage"}],
            "capture failures",
        ),
    ],
)
def test_load_runs_rejects_incomplete_object_envelopes(
    tmp_path,
    complete,
    failures,
    message,
):
    path = tmp_path / "runs.json"
    path.write_text(
        json.dumps(
            {
                "complete": complete,
                "failures": failures,
                "results": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        load_runs(path)


def test_load_runs_rejects_non_scored_capture_even_if_envelope_claims_complete(
    tmp_path,
):
    ranking = _depth_50_ranking(["relevant.py"])
    path = tmp_path / "runs.json"
    path.write_text(
        json.dumps(
            {
                "complete": True,
                "failures": [],
                "results": [
                    {
                        "query_id": "q1",
                        "variants": {
                            "baseline": _variant_with_depth(ranking),
                            "lfm_rerank": _variant_with_depth(ranking),
                        },
                        "late_interaction": {
                            "status": "partial",
                            "reason": "insufficient_candidate_coverage",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="is not scored"):
        load_runs(path)


def test_load_runs_rejects_envelope_with_missing_runtime_evidence(tmp_path):
    ranking = _depth_50_ranking(["relevant.py"])
    path = tmp_path / "runs.json"
    path.write_text(
        json.dumps(
            {
                "complete": True,
                "failures": [],
                "results": [
                    {
                        "query_id": "q1",
                        "variants": {
                            "baseline": _variant_with_depth(ranking),
                            "lfm_rerank": _variant_with_depth(ranking),
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requires late_interaction evidence"):
        load_runs(path)


def test_load_runs_preserves_and_validates_variant_depth_evidence(tmp_path):
    ranking = _depth_50_ranking(["relevant.py"])
    path = tmp_path / "runs.json"
    path.write_text(
        json.dumps(
            {
                "complete": True,
                "failures": [],
                "results": [
                    {
                        "query_id": "q1",
                        "variants": {
                            "baseline": _variant_with_depth(ranking, corpus_size=321),
                            "lfm_rerank": _variant_with_depth(ranking, corpus_size=321),
                        },
                        "late_interaction": {"status": "scored"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    runs = load_runs(path)

    assert runs["q1"]["baseline"]["depth"] == {
        "requested": 50,
        "returned_unique": 50,
        "corpus_size": 321,
        "status": "sufficient",
        "recall_at_50_eligible": True,
    }


def test_load_runs_rejects_false_sufficient_depth_metadata(tmp_path):
    short = _depth_50_ranking(["relevant.py"])[:10]
    path = tmp_path / "runs.json"
    path.write_text(
        json.dumps(
            {
                "complete": True,
                "failures": [],
                "results": [
                    {
                        "query_id": "q1",
                        "variants": {
                            "baseline": _variant_with_depth(short),
                            "lfm_rerank": _variant_with_depth(short),
                        },
                        "late_interaction": {"status": "scored"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="sufficient depth status requires"):
        load_runs(path)


def test_paired_eval_does_not_label_shallow_rankings_as_recall_at_50():
    dataset = load_dataset(
        ROOT / "eval" / "golden_set.json",
        ROOT / "eval" / "lfm_eval_extension.json",
    )
    runs = {
        entry.query_id: {
            "baseline": _depth_50_ranking(list(entry.expected_files))[:10],
            "lfm_rerank": _depth_50_ranking(list(entry.expected_files))[:10],
        }
        for entry in dataset
    }

    report = evaluate_paired(dataset, runs)

    assert report["aggregate"]["baseline"]["recall@50"] is None
    assert report["aggregate"]["lfm_rerank"]["recall@50"] is None
    assert report["paired"]["lfm_rerank"]["median_deltas"]["recall@50"] is None
    assert report["lfm_rerank_gate"]["checks"]["recall_at_50_evidence_complete"] is False
    assert report["lfm_rerank_gate"]["checks"]["recall_at_50_non_regression"] is False
    assert report["lfm_rerank_gate"]["passed"] is False


def test_paired_eval_reports_small_corpus_as_explicit_insufficient_evidence():
    dataset = load_dataset(
        ROOT / "eval" / "golden_set.json",
        ROOT / "eval" / "lfm_eval_extension.json",
    )
    runs = {}
    for entry in dataset:
        short = _depth_50_ranking(list(entry.expected_files))[:20]
        variant = _variant_with_depth(
            short,
            status="insufficient_corpus_depth",
            corpus_size=20,
        )
        runs[entry.query_id] = {
            "baseline": variant,
            "lfm_rerank": variant,
        }

    report = evaluate_paired(dataset, runs)

    assert report["evidence"]["depth_status_counts"]["baseline"] == {
        "insufficient_corpus_depth": 50
    }
    assert report["evidence"]["recall_at_50_eligible_counts"]["baseline"] == 0
    assert report["aggregate"]["baseline"]["recall@50"] is None
    assert report["lfm_rerank_gate"]["passed"] is False


def test_fastplaid_gate_requires_full_dataset_paired_coverage():
    dataset = load_dataset(
        ROOT / "eval" / "golden_set.json",
        ROOT / "eval" / "lfm_eval_extension.json",
    )
    runs = {}
    for index, entry in enumerate(dataset):
        baseline = _depth_50_ranking(
            list(entry.expected_files),
            first_distractor=True,
        )
        lfm = _depth_50_ranking(list(entry.expected_files))
        variants = {"baseline": baseline, "lfm_rerank": lfm}
        if index < len(dataset) - 1:
            variants["fastplaid"] = lfm
        runs[entry.query_id] = variants

    report = evaluate_paired(dataset, runs)

    assert report["evidence"]["variant_query_counts"]["fastplaid"] == 49
    assert report["paired"]["fastplaid"]["count"] == 49
    assert report["fastplaid_gate"]["checks"]["dataset_coverage_complete"] is False
    assert report["fastplaid_gate"]["checks"]["paired_query_count_complete"] is False
    assert report["fastplaid_gate"]["passed"] is False
    assert report["lfm_rerank_gate"]["passed"] is True
    assert report["overall_gate"]["passed"] is False


def test_live_paired_capture_extracts_rankings_and_runtime_metadata():
    record = result_record(
        "q-live",
        SimpleNamespace(
            selected_paths=["baseline-fallback.py"],
            debug={
                "late_interaction": {
                    "baseline_top_k": ["baseline.py"],
                    "counterfactual_top_k": ["lfm.py"],
                    "status": "scored",
                    "coverage": 1.0,
                    "latency_ms": 42.5,
                    "model_revision": "59633c2e",
                    "index_revision": "r1294",
                }
            },
        ),
    )

    assert record["variants"]["baseline"]["ranking"] == [{"path": "baseline.py"}]
    assert record["variants"]["lfm_rerank"]["ranking"] == [{"path": "lfm.py"}]
    assert record["variants"]["baseline"]["depth"] == {
        "requested": 50,
        "returned_unique": 1,
        "corpus_size": None,
        "status": "insufficient_returned_depth",
    }
    assert record["late_interaction"] == {
        "status": "scored",
        "coverage": 1.0,
        "latency_ms": 42.5,
        "model_revision": "59633c2e",
        "index_revision": "r1294",
        "reason": "",
    }


def test_schema_v2_paired_eval_rejects_mixed_index_revisions(tmp_path):
    ranking = _depth_50_ranking(["relevant.py"])
    path = tmp_path / "runs.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "complete": True,
                "failures": [],
                "evidence": {
                    "expected_index_revision": "r1294",
                    "expected_lineage_id": "lineage-r1",
                    "observed_lineage_id": "lineage-r1",
                    "expected_identity_digest": "e" * 64,
                    "observed_identity_digest": "e" * 64,
                    "expected_document_count": 1294,
                    "observed_document_count": 1294,
                },
                "results": [
                    {
                        "query_id": "q1",
                        "variants": {
                            "baseline": _variant_with_depth(ranking),
                            "lfm_rerank": _variant_with_depth(ranking),
                        },
                        "late_interaction": {
                            "status": "scored",
                            "index_revision": "r1295",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not match exact"):
        load_runs(path)


def test_live_paired_capture_marks_missing_telemetry_and_preserves_baseline():
    record = result_record(
        "q-missing",
        SimpleNamespace(selected_paths=["dense.py"], debug=None),
    )

    assert record["variants"]["baseline"]["ranking"] == [{"path": "dense.py"}]
    assert "lfm_rerank" not in record["variants"]
    assert record["late_interaction"]["status"] == "missing"


def test_runtime_parity_identical_rankings_pass_and_reversal_fails():
    corpus = ParityCorpus(
        queries=(ParityQuery("q1", "query", ("d1",)),),
        documents=(
            ParityDocument("d1", "relevant"),
            ParityDocument("d2", "other"),
            ParityDocument("d3", "other"),
        ),
    )
    reference = {
        "q1": [
            {"id": "d1", "score": 3.0},
            {"id": "d2", "score": 2.0},
            {"id": "d3", "score": 1.0},
        ]
    }
    identical = evaluate_parity(corpus, reference, reference, top_k=3)
    assert identical["summary"]["mean_top_k_overlap"] == 1.0
    assert identical["summary"]["mean_spearman"] == 1.0
    assert identical["summary"]["mean_kendall"] == 1.0
    assert identical["summary"]["max_abs_ndcg_delta"] == 0.0
    assert identical["gate"]["passed"] is True

    reversed_ranking = {
        "q1": [
            {"id": "d3", "score": 3.0},
            {"id": "d2", "score": 2.0},
            {"id": "d1", "score": 1.0},
        ]
    }
    reversed_report = evaluate_parity(corpus, reference, reversed_ranking, top_k=3)
    assert reversed_report["summary"]["mean_spearman"] == -1.0
    assert reversed_report["summary"]["mean_kendall"] == -1.0
    assert reversed_report["gate"]["passed"] is False
