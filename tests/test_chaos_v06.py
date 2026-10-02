"""Expanded v0.6.0 Chaos and Fault Injection Test Suite."""

from brain.harness.attestation import create_attestation
from brain.harness.models import ClaimCategory, EvidenceAuthority, EvidenceState, ReportClaim
from brain.harness.verifier import verify_claim
from brain.routing.policy_engine import SelectivePolicyEngine


# C1: Process Timeout & SIGKILL
def test_c1_process_sigkill_rejection():
    att = create_attestation("chaos-c1-1", "remote", ["python3", "main.py"], 137, "", "Killed", "t1", "t2", 0.05)
    claim = ReportClaim(claim_id="chaos-1", claim_text="Killed process", claim_category=ClaimCategory.INTEGRITY, required_evidence_types=[EvidenceAuthority.REMOTE_EXECUTOR], supplied_attestation_ids=[att.attestation_id])
    assert verify_claim(claim, [att]) == EvidenceState.FAILED


# C2: HTTP 429 Rate Limiting
def test_c2_http_429_rate_limit_handled():
    att = create_attestation("chaos-c2-1", "remote", ["curl"], 429, "", "HTTP 429 Too Many Requests", "t1", "t2", 1.0)
    claim = ReportClaim(claim_id="chaos-2", claim_text="429 Rate Limit", claim_category=ClaimCategory.DEPLOYMENT, required_evidence_types=[EvidenceAuthority.REMOTE_EXECUTOR], supplied_attestation_ids=[att.attestation_id])
    assert verify_claim(claim, [att]) == EvidenceState.FAILED


# C3: SSH Socket Disconnect
def test_c3_ssh_disconnect_rejection():
    att = create_attestation("chaos-c3-1", "remote", ["ssh"], 255, "", "Connection reset by peer", "t1", "t2", 2.0)
    claim = ReportClaim(claim_id="chaos-3", claim_text="SSH drop", claim_category=ClaimCategory.DEPLOYMENT, required_evidence_types=[EvidenceAuthority.REMOTE_EXECUTOR], supplied_attestation_ids=[att.attestation_id])
    assert verify_claim(claim, [att]) == EvidenceState.FAILED


# C4: Disk Full / Read-Only Storage
def test_c4_disk_full_rejection():
    att = create_attestation("chaos-c4-1", "local", ["tar"], 1, "", "No space left on device", "t1", "t2", 0.3)
    claim = ReportClaim(claim_id="chaos-4", claim_text="Disk full", claim_category=ClaimCategory.INTEGRITY, required_evidence_types=[EvidenceAuthority.LOCAL_EXECUTOR], supplied_attestation_ids=[att.attestation_id])
    assert verify_claim(claim, [att]) == EvidenceState.FAILED


# C5: Network Split-Brain Partition
def test_c5_split_brain_partition():
    att = create_attestation("chaos-c5-1", "remote", ["pg_isready"], 2, "", "could not connect to server", "t1", "t2", 5.0)
    claim = ReportClaim(claim_id="chaos-5", claim_text="Split brain", claim_category=ClaimCategory.OPERATIONS, required_evidence_types=[EvidenceAuthority.REMOTE_EXECUTOR], supplied_attestation_ids=[att.attestation_id])
    assert verify_claim(claim, [att]) == EvidenceState.FAILED


# C6: Selective Utility Gate Precision/Recall Threshold
def test_c6_selective_gate_fails_low_precision():
    passed, msg = SelectivePolicyEngine.validate_precision_recall_gate(0.75, 0.85, 3, 8.0)
    assert not passed
    assert "Precision" in msg


def test_c6_selective_gate_passes_high_precision():
    passed, msg = SelectivePolicyEngine.validate_precision_recall_gate(0.85, 0.90, 3, 8.0)
    assert passed
    assert "PASSED" in msg
