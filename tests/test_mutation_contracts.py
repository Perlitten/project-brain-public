"""Observable security and transport contracts, independent of implementation formulas."""
import hashlib
import json

import pytest

from brain.context.budget import ContextBudget, bounded_join, estimated_tokens, truncate_utf8
from brain.harness.attestation import create_attestation, generate_signed_attestation, verify_hmac_attestation
from brain.harness.nonce import RemoteNonceManager


def _signed():
    return generate_signed_attestation(
        "execution", "challenge", ["python", "-m", "pytest"], 0, "passed", "", 0.2,
        "image-digest", "container-id", secret_key="test-only-key",
    )


def test_hmac_requires_signature_scheme_nonce_and_correct_key():
    attestation = _signed()
    assert verify_hmac_attestation(attestation, "challenge", "test-only-key")
    assert not verify_hmac_attestation(attestation, "other-challenge", "test-only-key")
    assert not verify_hmac_attestation(attestation, "challenge", "other-key")
    attestation.attestation_hash = attestation.calculate_hash()
    assert not verify_hmac_attestation(attestation, "challenge", "test-only-key")


@pytest.mark.parametrize("field,value", [
    ("exit_code", 17), ("sanitized_argv", ["different", "command"]),
    ("source_tree_sha", "changed-tree"), ("image_id", "changed-image"),
    ("previous_attestation_hash", "changed-parent"), ("remote_nonce", "changed-nonce"),
])
def test_hmac_binds_execution_identity_and_result(field, value):
    attestation = _signed()
    setattr(attestation, field, value)
    assert not verify_hmac_attestation(attestation, "challenge", "test-only-key")


def test_self_hash_is_full_sha256_over_canonical_payload():
    attestation = create_attestation("execution", "local", ["run"], 0, "output", "", "start", "end", 1)
    payload = attestation.model_dump()
    payload.pop("attestation_hash")
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    assert attestation.attestation_hash == hashlib.sha256(canonical).hexdigest()
    assert len(attestation.attestation_hash) == 64


def test_nonce_expiry_is_independent_of_other_challenges(monkeypatch):
    import brain.harness.nonce as module

    now = [100.0]
    # Patch both APIs: the expiry contract survives switching to a monotonic clock.
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    manager = RemoteNonceManager(ttl_seconds=10)
    expired = manager.generate_nonce()
    now[0] = 105.0
    current = manager.generate_nonce()
    now[0] = 111.0
    assert not manager.verify_and_consume(expired)
    assert not manager.verify_and_consume(expired)
    assert not manager.verify_and_consume("never-issued")
    assert not manager.verify_and_consume("")
    assert manager.verify_and_consume(current)
    assert not manager.verify_and_consume(current)


def test_nonce_inclusive_ttl_boundary(monkeypatch):
    import brain.harness.nonce as module

    now = [100.0]
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    manager = RemoteNonceManager(ttl_seconds=10)
    nonce = manager.generate_nonce()
    now[0] = 110.0
    assert manager.verify_and_consume(nonce)


def test_reissued_consumed_nonce_cannot_be_replayed(monkeypatch):
    import brain.harness.nonce as module

    # A repeated RNG output must not resurrect a challenge already consumed.
    monkeypatch.setattr(module.secrets, "token_hex", lambda size: "a" * (size * 2))
    manager = RemoteNonceManager()
    nonce = manager.generate_nonce()
    assert manager.verify_and_consume(nonce)
    assert manager.generate_nonce() == nonce
    assert not manager.verify_and_consume(nonce)


@pytest.mark.parametrize("count,expected", [(-5, 0), (0, 0), (1, 1), (4, 1), (5, 2), (8, 2), (9, 3)])
def test_token_reporting_rounds_up_byte_quarters(count, expected):
    assert estimated_tokens(count) == expected


@pytest.mark.parametrize("text,cap,suffix,expected", [
    ("hello", 5, "…", "hello"), ("hello", 6, "…", "hello"),
    ("hello", 4, "…", "h…"), ("hello", 3, "…", "…"),
    ("hello", 2, "…", "he"), ("🙂rest", 2, "…", ""),
    ("ééé", 4, "!", "é!"), ("hello", 2, "", "he"),
    ("hello", 0, "…", ""), ("hello", -1, "…", ""),
])
def test_truncation_preserves_exact_fit_and_useful_unicode_prefix(text, cap, suffix, expected):
    assert truncate_utf8(text, cap, suffix) == expected


def test_budget_accounts_for_multibyte_content_and_preserves_snapshot():
    budget = ContextBudget(10, allocations={"code": 6, "notes": 4})
    assert budget.add_text("code", "ééé") == "ééé"
    assert budget.remaining_bytes == 4
    assert budget.add_text("notes", "four") == "four"
    assert budget.remaining_bytes == 0
    assert budget.snapshot() == {
        "estimated_tokens": 3, "bytes": 10, "limit": 10, "truncated": False,
        "sections": {"code": 6, "notes": 4}, "excluded": [],
    }
    snapshot = budget.snapshot()
    snapshot["sections"]["code"] = 999
    assert budget.snapshot()["sections"]["code"] == 6
    assert budget.add_text("extra", "no room") == ""
    assert budget.snapshot()["excluded"] == [{"section": "extra", "reason": "byte_cap"}]


def test_explicit_allocation_can_only_reduce_section_allowance():
    budget = ContextBudget(100, allocations={"code": 8})
    assert budget.add_text("code", "abcdef", allocation=4) == "a…"
    assert budget.consumed_bytes == 4
    budget = ContextBudget(100, allocations={"code": 4})
    assert budget.add_text("code", "abcdef", allocation=8) == "a…"


@pytest.mark.parametrize("cap,allocations", [(0, {}), (-1, {}), (10, {"code": -1})])
def test_invalid_budget_is_rejected(cap, allocations):
    with pytest.raises(ValueError):
        ContextBudget(cap, allocations=allocations)


def test_zero_allocation_is_valid_and_excludes_content():
    budget = ContextBudget(10, allocations={"code": 0})
    assert budget.add_text("code", "content") == ""
    assert budget.snapshot()["truncated"]


def test_exclusion_metadata_has_bounded_count_and_labels():
    budget = ContextBudget(100)
    for _ in range(40):
        budget.exclude("x" * 100, "y" * 200)
    exclusions = budget.snapshot()["excluded"]
    assert len(exclusions) == 24
    assert exclusions == [{"section": "x" * 80, "reason": "y" * 120}] * 24


def test_join_retains_ordered_items_at_exact_limit_without_consuming_overflow():
    budget = ContextBudget(10, allocations={"code": 5})
    assert bounded_join(budget, "code", ["ab", "cd", "ef"], separator="|") == "ab|cd"
    assert budget.consumed_bytes == 5
    assert budget.snapshot()["truncated"]
    assert bounded_join(ContextBudget(5), "code", ["ab", "cd"], separator="|") == "ab|cd"
    budget = ContextBudget(10, allocations={"code": 9})
    assert budget.add_text("other", "123456") == "123456"
    assert bounded_join(budget, "code", ["ab", "cd"], separator="|") == "ab"
    assert budget.consumed_bytes == 8
