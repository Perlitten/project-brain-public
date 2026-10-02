"""Fault Injection and Chaos Test Suite for Project Brain v0.5.4."""

from brain.harness.attestation import create_attestation
from brain.harness.models import ClaimCategory, EvidenceAuthority, EvidenceState, ReportClaim
from brain.harness.verifier import verify_claim


# D1: Process Failures
def test_d1_process_kill_before_execution():
    att = create_attestation("exec-kill-1", "local", ["bash"], 137, "", "SIGKILL received", "t1", "t2", 0.1)
    claim = ReportClaim(claim_id="d1", claim_text="Exec killed", claim_category=ClaimCategory.INTEGRITY, required_evidence_types=[EvidenceAuthority.LOCAL_EXECUTOR], supplied_attestation_ids=[att.attestation_id])
    assert verify_claim(claim, [att]) == EvidenceState.FAILED


def test_d1_process_timeout():
    att = create_attestation("exec-timeout-1", "local", ["bash"], 124, "", "Command timed out after 300s", "t1", "t2", 300.0)
    claim = ReportClaim(claim_id="d1_2", claim_text="Exec timeout", claim_category=ClaimCategory.INTEGRITY, required_evidence_types=[EvidenceAuthority.LOCAL_EXECUTOR], supplied_attestation_ids=[att.attestation_id])
    assert verify_claim(claim, [att]) == EvidenceState.FAILED


# D2: Network Failures
def test_d2_network_dns_failure():
    att = create_attestation("net-dns-1", "remote", ["curl"], 6, "", "Could not resolve host", "t1", "t2", 1.0)
    claim = ReportClaim(claim_id="d2_1", claim_text="DNS fail", claim_category=ClaimCategory.DEPLOYMENT, required_evidence_types=[EvidenceAuthority.REMOTE_EXECUTOR], supplied_attestation_ids=[att.attestation_id])
    assert verify_claim(claim, [att]) == EvidenceState.FAILED


def test_d2_ssh_authentication_failure():
    att = create_attestation("ssh-auth-1", "remote", ["ssh"], 255, "", "Permission denied (publickey)", "t1", "t2", 1.0)
    claim = ReportClaim(claim_id="d2_2", claim_text="SSH auth fail", claim_category=ClaimCategory.DEPLOYMENT, required_evidence_types=[EvidenceAuthority.REMOTE_EXECUTOR], supplied_attestation_ids=[att.attestation_id])
    assert verify_claim(claim, [att]) == EvidenceState.FAILED


# D3: Storage Failures
def test_d3_storage_permission_denied():
    att = create_attestation("store-perm-1", "local", ["mkdir"], 1, "", "Permission denied", "t1", "t2", 0.1)
    claim = ReportClaim(claim_id="d3_1", claim_text="Store perm fail", claim_category=ClaimCategory.INTEGRITY, required_evidence_types=[EvidenceAuthority.LOCAL_EXECUTOR], supplied_attestation_ids=[att.attestation_id])
    assert verify_claim(claim, [att]) == EvidenceState.FAILED


# D4: Database Failures
def test_d4_postgres_unavailable():
    att = create_attestation("db-pg-1", "local", ["pg_dump"], 1, "", "could not connect to server: Connection refused", "t1", "t2", 0.5)
    claim = ReportClaim(claim_id="d4_1", claim_text="Postgres down", claim_category=ClaimCategory.OPERATIONS, required_evidence_types=[EvidenceAuthority.LOCAL_EXECUTOR], supplied_attestation_ids=[att.attestation_id])
    assert verify_claim(claim, [att]) == EvidenceState.FAILED


# D5: Deployment Failures
def test_d5_image_build_failure():
    att = create_attestation("dep-build-1", "remote", ["docker", "build"], 1, "", "Dockerfile line 12: command exited with code 1", "t1", "t2", 15.0)
    claim = ReportClaim(claim_id="d5_1", claim_text="Docker build fail", claim_category=ClaimCategory.DEPLOYMENT, required_evidence_types=[EvidenceAuthority.REMOTE_EXECUTOR], supplied_attestation_ids=[att.attestation_id])
    assert verify_claim(claim, [att]) == EvidenceState.FAILED
