from dataclasses import dataclass

import pytest

from brain.context.evidence_support import (
    query_evidence_support,
    should_abstain_for_unsupported_query,
)


@dataclass
class Candidate:
    path: str
    symbols: tuple[str, ...] = ()


def test_unsupported_multi_anchor_query_abstains_without_phrase_blacklist():
    candidates = [Candidate("src/config.py", ("configure_file",)), Candidate("src/ui.tsx")]
    result = query_evidence_support("Kubernetes mobile deployment", candidates)
    assert result["decided"] is True
    assert result["supported_terms"] == ()
    assert should_abstain_for_unsupported_query("Kubernetes mobile deployment", candidates)


def test_generic_locator_match_does_not_count_as_domain_support():
    candidates = [Candidate("src/runtime.py", ("implement",)), Candidate("src/app.py")]
    result = query_evidence_support("explain rollout native ios", candidates)
    assert result["supported_terms"] == ()
    assert result["should_abstain"] is True


def test_slice_content_can_support_a_concept_without_matching_filename():
    candidates = [Candidate("src/store.py"), Candidate("src/repository.py")]
    slices = [{"path": "src/store.py", "content": "soft deletion cascade preserves children"}]
    result = query_evidence_support("soft deletion cascade", candidates, slices)
    assert set(result["supported_terms"]) == {"soft", "deletion", "cascade"}
    assert result["should_abstain"] is False


def test_explicit_api_symbols_support_a_query():
    candidates = [Candidate("apps/api/auth.py", ("validate_api_key", "credentials"))]
    assert not should_abstain_for_unsupported_query("credentials api", candidates)


def test_substring_inside_unrelated_identifier_is_not_support():
    candidates = [Candidate("brain/asyncio_helpers.py", ("native_runner",))]
    result = query_evidence_support("ios mobile", candidates)
    assert result["supported_terms"] == ()
    assert result["should_abstain"] is True


def test_non_latin_semantic_query_is_left_undecided():
    candidates = [Candidate("src/core.py", ("Core",))]
    result = query_evidence_support("上下文检查", candidates)
    assert result["decided"] is False
    assert result["should_abstain"] is False


def test_single_anchor_query_is_left_undecided():
    candidates = [Candidate("src/embedding.py", ("EmbeddingStore",))]
    assert not should_abstain_for_unsupported_query("embedding", candidates)


def test_real_status_and_parameter_identifiers_support_a_contract_question():
    candidates = [Candidate("brain/context/runtime_context_builder.py")]
    slices = [{"path": candidates[0].path, "content": '''
if not persist:
    return {"status": "stale_blocked", "context": False}
'''}]
    query = "What does stale_blocked mean for a context response, and what does persist=false change about context storage?"
    result = query_evidence_support(query, candidates, slices)
    assert "stale_blocked" in result["supported_terms"]
    assert "persist" in result["supported_terms"]
    assert result["should_abstain"] is False


def test_quoted_prose_and_test_status_values_cannot_establish_a_feature():
    candidates = [Candidate("src/prompts.py"), Candidate("tests/test_runtime.py")]
    slices = [
        {"path": "src/prompts.py", "content": 'prompt = "Kubernetes native mobile deployment"'},
        {"path": "tests/test_runtime.py", "content": 'status = "kubernetes"\nmobile = True'},
    ]
    assert should_abstain_for_unsupported_query("Kubernetes native mobile deployment", candidates, slices)

def test_existing_named_platform_requires_evidence_even_with_generic_support():
    from brain.context.evidence_support import should_abstain_for_unsupported_query
    from brain.retrieval.service import RetrievalCandidate
    candidates = [RetrievalCandidate("apps/api/deployment.py",symbols=["deploy_application"])]
    slices = [{"path":"apps/api/deployment.py","content":"def deploy_application(app): return app"}]
    assert should_abstain_for_unsupported_query("How does ZebraCloud deploy the application?",candidates,slices)
    assert not should_abstain_for_unsupported_query("Add ZebraCloud deployment support",candidates,slices)


def test_question_prefix_with_long_whitespace_keeps_named_evidence_guard():
    candidates = [Candidate("apps/api/deployment.py", ("deploy_application",))]
    slices = [{"path": candidates[0].path, "content": "def deploy_application(app): return app"}]
    prefix = " " * 100_000
    assert should_abstain_for_unsupported_query(prefix + "How does ZebraCloud deploy the application?", candidates, slices)
    assert not should_abstain_for_unsupported_query(prefix + "Add ZebraCloud deployment support", candidates, slices)


@pytest.mark.parametrize("language,extension", [("Python", "py"), ("TypeScript", "ts"), ("JavaScript", "js"), ("Kotlin", "kt"), ("Rust", "rs")])
def test_source_extension_supports_a_named_language_question(language, extension):
    candidates = [Candidate(f"src/config.{extension}", ("load_configuration",))]
    result = query_evidence_support(f"How does {language} load configuration?", candidates)
    assert language.casefold() in result["supported_terms"]
    assert not result["should_abstain"]
    assert not result["missing_named_anchors"]


def test_language_evidence_does_not_establish_an_unknown_platform():
    candidates = [Candidate("src/config.py", ("load_configuration",))]
    result = query_evidence_support("How does ZebraCloud load Python configuration?", candidates)
    assert result["should_abstain"]
    assert "ZebraCloud" in result["missing_named_anchors"]


def test_language_extensions_in_test_fixtures_are_not_feature_evidence():
    candidates = [Candidate("tests/config.py", ("load_configuration",))]
    assert should_abstain_for_unsupported_query("How does Python load configuration?", candidates)
