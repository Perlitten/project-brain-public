"""State Machine Property and Patch Quality Adversary Tests."""

from brain.harness.attestation import create_attestation
from brain.harness.models import ClaimCategory, EvidenceAuthority, EvidenceState, ReportClaim
from brain.harness.verifier import verify_claim


# E1: State Machine Properties
def test_terminal_states_never_become_verified():
    att = create_attestation("term-1", "local", ["run"], 1, "failed", "", "t1", "t2", 1.0)
    claim = ReportClaim(claim_id="prop-1", claim_text="Test", claim_category=ClaimCategory.INTEGRITY, required_evidence_types=[EvidenceAuthority.LOCAL_EXECUTOR], supplied_attestation_ids=[att.attestation_id])
    res = verify_claim(claim, [att])
    assert res != EvidenceState.VERIFIED
    assert claim.verification_status != EvidenceState.VERIFIED


def test_hash_tampering_detected():
    att = create_attestation("hash-1", "local", ["run"], 0, "ok", "", "t1", "t2", 1.0)
    att.exit_code = 99  # tamper after signing
    assert not att.verify_integrity()


# G1: Patch Quality Adversaries
def test_patch_quality_empty_patch():
    att = create_attestation("patch-1", "local", ["git", "apply"], 1, "", "patch is empty", "t1", "t2", 0.1)
    claim = ReportClaim(claim_id="patch-c1", claim_text="Empty patch", claim_category=ClaimCategory.INTEGRITY, required_evidence_types=[EvidenceAuthority.LOCAL_EXECUTOR], supplied_attestation_ids=[att.attestation_id])
    assert verify_claim(claim, [att]) == EvidenceState.FAILED
