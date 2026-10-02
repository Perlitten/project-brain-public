"""Unseen 12-Task Selective Holdout Suite for Project Brain v0.5.3."""

from typing import Dict, Any

SELECTIVE_HOLDOUT_V053_TASKS: Dict[str, Dict[str, Any]] = {
    "task_s1_trivial_doc": {
        "title": "Fix docstring formatting in logging utility",
        "category": "trivial_localized",
        "predeclared_expected_route": "legacy_no_brain",
        "multi_repo": False,
        "repo_count": 1,
    },
    "task_s2_trivial_format": {
        "title": "Clean trailing whitespace in settings loader",
        "category": "trivial_localized",
        "predeclared_expected_route": "legacy_no_brain",
        "multi_repo": False,
        "repo_count": 1,
    },
    "task_s3_trivial_constant": {
        "title": "Update default max retries constant in client",
        "category": "trivial_localized",
        "predeclared_expected_route": "legacy_no_brain",
        "multi_repo": False,
        "repo_count": 1,
    },
    "task_s4_multifile_feature": {
        "title": "Add rate limiting headers to API routes and config",
        "category": "multi_file_feature",
        "predeclared_expected_route": "phased_brain_selective",
        "multi_repo": True,
        "repo_count": 2,
    },
    "task_s5_multifile_metric": {
        "title": "Track latency histograms across worker and API",
        "category": "multi_file_feature",
        "predeclared_expected_route": "phased_brain_selective",
        "multi_repo": True,
        "repo_count": 2,
    },
    "task_s6_multifile_filtering": {
        "title": "Add category search filters to dashboard and backend",
        "category": "multi_file_feature",
        "predeclared_expected_route": "phased_brain_selective",
        "multi_repo": True,
        "repo_count": 2,
    },
    "task_s7_arch_boundary_events": {
        "title": "Enforce event stream boundaries between core and ledger",
        "category": "architecture_boundary",
        "predeclared_expected_route": "phased_brain_selective",
        "multi_repo": True,
        "repo_count": 2,
    },
    "task_s8_arch_boundary_auth": {
        "title": "Isolate auth token session store from public API",
        "category": "architecture_boundary",
        "predeclared_expected_route": "phased_brain_selective",
        "multi_repo": False,
        "repo_count": 1,
    },
    "task_s9_cross_repo_contract_sync": {
        "title": "Align API response schema contract with frontend client",
        "category": "cross_repo_contract",
        "predeclared_expected_route": "phased_brain_selective",
        "multi_repo": True,
        "repo_count": 3,
    },
    "task_s10_cross_repo_proto_sync": {
        "title": "Propagate proto schema updates to worker protocol",
        "category": "cross_repo_contract",
        "predeclared_expected_route": "phased_brain_selective",
        "multi_repo": True,
        "repo_count": 2,
    },
    "task_s11_structured_test_repair": {
        "title": "Repair failing integration test assertion in database mock",
        "category": "structured_test_repair",
        "predeclared_expected_route": "phased_no_brain",
        "multi_repo": False,
        "repo_count": 1,
    },
    "task_s12_insufficient_evidence": {
        "title": "Refactor non-existent legacy V0 module without source",
        "category": "insufficient_evidence",
        "predeclared_expected_route": "abstain_stale",
        "multi_repo": False,
        "repo_count": 1,
    },
}
