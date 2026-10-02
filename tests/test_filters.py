"""Tests for retrieval filter helpers."""

from brain.search.filters import (
    HISTORICAL_AUTHORITY_NOTE,
    HISTORICAL_RETRIEVAL_WEIGHT,
    KNOWLEDGE_STATUS_CURRENT,
    KNOWLEDGE_STATUS_HISTORICAL,
    annotate_knowledge_summary,
    classify_knowledge_status,
    get_contextual_retrieval_weight,
    is_template_path,
    is_test_path,
    knowledge_authority_weight,
    knowledge_status_from_summary,
    query_requests_historical_context,
    trim_ranked_files,
)


def test_template_path_detection():
    assert is_template_path("apps/api/templates/base.html")
    assert not is_template_path("apps/api/main.py")


def test_test_path_detection():
    assert is_test_path("tests/test_health.py")
    assert not is_test_path("apps/api/main.py")


def test_explicit_historical_frontmatter_is_classified_without_age_guessing():
    historical = """---
status: historical
updated: 2026-07-23
---
# Old device walkthrough
"""
    current = """# ADR-001

- Status: Accepted
- Date: 2024-01-01
"""

    assert (
        classify_knowledge_status("docs/ux/old.md", historical)
        == KNOWLEDGE_STATUS_HISTORICAL
    )
    assert (
        classify_knowledge_status("docs/adr/ADR-001.md", current)
        == KNOWLEDGE_STATUS_CURRENT
    )
    assert (
        classify_knowledge_status("src/history.py", "status: historical")
        == KNOWLEDGE_STATUS_CURRENT
    )
    assert (
        classify_knowledge_status(
            "docs/current.md",
            "# Current contract\n\n```yaml\nstatus: historical\n```\n",
        )
        == KNOWLEDGE_STATUS_CURRENT
    )


def test_historical_header_marker_is_classified():
    content = (
        "# Migration plan\n\n"
        "> **Historical planning artifact.** This is not the current topology.\n"
    )
    assert (
        classify_knowledge_status("docs/plans/migration.md", content)
        == KNOWLEDGE_STATUS_HISTORICAL
    )


def test_snapshot_and_task_contract_paths_are_non_current_evidence():
    for path in (
        "docs/audits/product-readiness/report.md",
        "docs/design/sources/prototype/index.html",
        ".agent-factory/tasks/NOSTIA-001.json",
    ):
        assert (
            classify_knowledge_status(path, "Current-looking content")
            == KNOWLEDGE_STATUS_HISTORICAL
        )

    assert (
        classify_knowledge_status(
            "docs/architecture/golden-build.md",
            "Current contract",
        )
        == KNOWLEDGE_STATUS_CURRENT
    )


def test_historical_summary_annotation_is_explicit_and_idempotent():
    annotated = annotate_knowledge_summary(
        "A dated device walkthrough.",
        KNOWLEDGE_STATUS_HISTORICAL,
    )
    assert HISTORICAL_AUTHORITY_NOTE in annotated
    assert knowledge_status_from_summary(annotated) == KNOWLEDGE_STATUS_HISTORICAL
    assert (
        annotate_knowledge_summary(annotated, KNOWLEDGE_STATUS_HISTORICAL)
        == annotated
    )
    assert (
        annotate_knowledge_summary("Current contract.", KNOWLEDGE_STATUS_CURRENT)
        == "Current contract."
    )


def test_historical_evidence_is_demoted_unless_explicitly_requested():
    summary = annotate_knowledge_summary(
        "Old runtime.",
        KNOWLEDGE_STATUS_HISTORICAL,
    )
    assert knowledge_authority_weight(summary, "current production runtime") == (
        HISTORICAL_RETRIEVAL_WEIGHT
    )
    assert knowledge_authority_weight(summary, "historical production runtime") == 1.0
    assert knowledge_authority_weight(summary, "история старой топологии") == 1.0
    assert knowledge_authority_weight("Current runtime.", "current runtime") == 1.0
    assert query_requests_historical_context("show the previous decision context")
    assert query_requests_historical_context("show the audit evidence")
    assert query_requests_historical_context("load task contract NOSTIA-001")
    assert not query_requests_historical_context("remove the old version")


def test_contextual_weight_demotes_templates_for_non_design_tasks():
    weight = get_contextual_retrieval_weight(
        "apps/api/templates/logs.html",
        "source_code",
        "feature",
        "Fix health check endpoint",
    )
    assert weight < 0.3


def test_contextual_weight_boosts_templates_for_design_tasks():
    weight = get_contextual_retrieval_weight(
        "apps/api/templates/base.html",
        "source_code",
        "design_change",
        "Update dashboard HTML branding",
    )
    assert weight >= 1.0


def test_trim_ranked_files_drops_low_score_tail():
    scored = [
        (1.0, "a.py", {}),
        (0.9, "b.py", {}),
        (0.8, "c.py", {}),
        (0.1, "noise.py", {}),
        (0.05, "more_noise.py", {}),
    ]
    trimmed = trim_ranked_files(scored, file_limit=10, min_keep=2, score_ratio=0.38)
    paths = [item[1] for item in trimmed]
    assert "a.py" in paths
    assert "b.py" in paths
    assert "noise.py" not in paths


def test_apply_pack_slot_policy_protects_pipeline_top10():
    from brain.search.filters import apply_pack_slot_policy

    entries = [
        (1.0, "src/engine/a.py", {}),
        (0.9, "tests/test_a.py", {}),
        (0.8, "tests/test_b.py", {}),
        (0.7, "tests/test_c.py", {}),
        (0.6, "tests/test_d.py", {}),
        (0.5, "tests/test_e.py", {}),
        (0.4, "tests/test_f.py", {}),
    ]
    protected = {"src/engine/a.py", "tests/test_a.py"}
    capped, exclusions = apply_pack_slot_policy(
        entries, 10, protected, "other", "unrelated feature work", "standard"
    )
    paths = [e[1] for e in capped]
    assert "tests/test_a.py" in paths
    assert len([p for p in paths if p.startswith("tests/")]) < len(entries)
    assert any(reason == "test_slot_cap" for reason in exclusions.values())
