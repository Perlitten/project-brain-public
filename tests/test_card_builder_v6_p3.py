"""Tests for Retrieval v6 P3 file-card construction + embedding provider."""

from brain.config.settings import settings
from brain.embeddings.card_embeddings import get_card_embedding_provider
from brain.indexers.card_builder import (
    CARD_EXTRACTOR_VERSION,
    build_card,
    build_card_fields,
    derive_file_role,
)

ROUTER_SYMS = {
    "api_route": ["/api/ml/status", "/api/ml/simulate"],
    "function": ["ml_status", "run_simulation"],
    "import_target": ["src/engine/ai_engine.py"],
}
CLI_SYMS = {"cli_flag": ["snapshot-manifest", "output"], "script_entrypoint": ["build_snapshot"]}


def test_derive_file_role():
    assert derive_file_role("src/server/routers/ml.py", "server", ROUTER_SYMS) == "api_handler"
    assert derive_file_role("scripts/build_snapshot.py", "scripts", CLI_SYMS) == "cli_entrypoint"
    assert derive_file_role("tests/test_x.py", "tests", {}) == "test"
    assert derive_file_role("src/engine/ai_engine.py", "engine", {}) == "engine"
    assert derive_file_role("pyproject.toml", "config", {}) == "config"
    assert derive_file_role("data/cards.csv", "other", {}) == "data"


def test_build_card_code_shaped_contains_structure():
    card = build_card("src/server/routers/ml.py", ROUTER_SYMS, fmt="code_shaped")
    assert card.file_role == "api_handler"
    assert card.surface == "server"
    assert "@route /api/ml/simulate" in card.card_text
    assert "def ml_status" in card.card_text
    assert "import src/engine/ai_engine.py" in card.card_text
    assert card.card_text_format == "code_shaped"


def test_build_card_prose_contains_structure():
    card = build_card("src/server/routers/ml.py", ROUTER_SYMS, fmt="prose")
    assert "api_handler" in card.card_text
    assert "/api/ml/simulate" in card.card_text
    assert card.card_text_format == "prose"


def test_card_hash_deterministic_and_format_sensitive():
    a = build_card("src/server/routers/ml.py", ROUTER_SYMS, fmt="code_shaped")
    b = build_card("src/server/routers/ml.py", ROUTER_SYMS, fmt="code_shaped")
    c = build_card("src/server/routers/ml.py", ROUTER_SYMS, fmt="prose")
    assert a.card_hash == b.card_hash  # deterministic
    assert a.card_hash != c.card_hash  # format-sensitive
    assert CARD_EXTRACTOR_VERSION in ("p3.1",)  # version pinned


def test_card_fields_domain_terms():
    f = build_card_fields("scripts/sync_hermes_rag.py", {"function": ["sync_vault"]})
    assert f["role"] == "script"
    assert any("hermes" in t or "rag" in t or "sync" in t for t in f["domain_terms"])


def test_card_embedding_provider_defaults_to_embedding_config():
    p = get_card_embedding_provider()
    # In P3 the card provider inherits the configured embedding provider.
    assert p.provider == settings.DEFAULT_EMBEDDING_PROVIDER.lower()
    assert p.dimension > 0


def test_card_flags_default_off():
    assert settings.RETRIEVAL_V6_FILE_CARDS_ENABLED is False
    assert settings.RETRIEVAL_CARD_TEXT_FORMAT in ("prose", "code_shaped")
