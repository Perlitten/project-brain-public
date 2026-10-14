"""Regression reproductions for defects uncovered during the mutation audit."""
import pytest

from brain.context.budget import ContextBudget, bounded_join
from brain.harness.attestation import create_attestation, generate_signed_attestation, verify_hmac_attestation
from brain.harness.models import ClaimCategory, EvidenceAuthority, EvidenceState, ReportClaim
from brain.harness.nonce import RemoteNonceManager
from brain.harness.verifier import verify_claim


def _attestation():
    return create_attestation("execution", "remote", ["run"], 0, "passed", "", "start", "end", 1,
                              image_id="image-digest")


def test_signature_prefix_cannot_authenticate_evidence_or_verify_a_claim():
    attestation = _attestation()
    attestation.attestation_hash = "hmac-sha256:forged"
    claim = ReportClaim(
        claim_id="claim", claim_text="Execution succeeded", claim_category=ClaimCategory.INTEGRITY,
        required_evidence_types=[EvidenceAuthority.REMOTE_EXECUTOR],
        supplied_attestation_ids=[attestation.attestation_id],
    )
    assert not attestation.verify_integrity()
    assert verify_claim(claim, [attestation]) == EvidenceState.FAILED
    assert claim.verification_status != EvidenceState.VERIFIED
    assert not claim.allowed_in_executive_summary


def test_signed_integrity_requires_explicit_credentials_and_binds_the_payload():
    attestation = generate_signed_attestation("execution", "challenge", ["run"], 0, "passed", "", 1,
                                             "image", "container", secret_key="test-only-key")
    assert not attestation.verify_integrity()
    assert attestation.verify_integrity(secret_key="test-only-key", expected_nonce="challenge")
    assert not attestation.verify_integrity(secret_key="wrong-key", expected_nonce="challenge")
    assert not attestation.verify_integrity(secret_key="test-only-key", expected_nonce="wrong-challenge")
    assert not attestation.verify_integrity(secret_key="", expected_nonce="challenge")
    assert not attestation.verify_integrity(expected_nonce="challenge")
    assert not attestation.verify_integrity(secret_key="test-only-key")
    attestation.exit_code = 1
    assert not attestation.verify_integrity(secret_key="test-only-key", expected_nonce="challenge")


@pytest.mark.parametrize("signature", ["é" * 64, "☃", "", "z" * 64, "a" * 63, "a" * 65])
def test_malformed_hmac_returns_false_without_raising(signature):
    attestation = _attestation()
    attestation.remote_nonce = "challenge"
    attestation.attestation_hash = "hmac-sha256:" + signature
    assert not verify_hmac_attestation(attestation, "challenge", "test-only-key")


def test_nonce_issued_at_clock_origin_is_valid(monkeypatch):
    import brain.harness.nonce as module

    monkeypatch.setattr(module.time, "time", lambda: 0.0)
    monkeypatch.setattr(module.time, "monotonic", lambda: 0.0)
    manager = RemoteNonceManager(ttl_seconds=10)
    nonce = manager.generate_nonce()
    assert manager.verify_and_consume(nonce)
    assert not manager.verify_and_consume(nonce)


def test_wall_clock_rollback_cannot_extend_nonce_lifetime(monkeypatch):
    import brain.harness.nonce as module

    wall_clock, elapsed = [1000.0], [100.0]
    monkeypatch.setattr(module.time, "time", lambda: wall_clock[0])
    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])
    manager = RemoteNonceManager(ttl_seconds=10)
    nonce = manager.generate_nonce()
    wall_clock[0], elapsed[0] = 900.0, 111.0
    assert not manager.verify_and_consume(nonce)


def test_multiple_additions_share_a_sections_allocation():
    budget = ContextBudget(100, allocations={"code": 6})
    assert budget.add_text("code", "abcd") == "abcd"
    assert budget.add_text("code", "efgh") == "ef"
    assert budget.add_text("code", "ijkl") == ""
    assert budget.sections["code"] == 6
    assert budget.consumed_bytes == 6
    assert budget.remaining_bytes == 94
    assert budget.snapshot()["truncated"]


def test_join_after_prior_content_keeps_whole_items_within_remaining_allocation():
    budget = ContextBudget(100, allocations={"code": 5})
    assert budget.add_text("code", "abcd") == "abcd"
    assert bounded_join(budget, "code", ["ef", "gh"], separator="|") == ""
    assert budget.sections["code"] == 4
    assert budget.snapshot()["truncated"]
