"""Unit tests for ExecutionAttestation, Nonce, Verifier, and Harness CLI."""

from brain.harness.attestation import create_attestation
from brain.harness.models import ClaimCategory, EvidenceAuthority, EvidenceState, ReportClaim
from brain.harness.nonce import RemoteNonceManager
from brain.harness.verifier import redact_secrets, is_placeholder_value, verify_claim


def test_attestation_integrity():
    att = create_attestation(
        execution_id="exec-123",
        executor_type="local",
        command_argv=["python3", "main.py"],
        exit_code=0,
        stdout_text="hello",
        stderr_text="",
        start_time_utc="2026-08-06T22:00:00Z",
        end_time_utc="2026-08-06T22:00:01Z",
        duration_s=1.0,
    )
    assert att.verify_integrity()
    att.exit_code = 1
    assert not att.verify_integrity()


def test_remote_nonce_challenge():
    mgr = RemoteNonceManager(ttl_seconds=60)
    nonce = mgr.generate_nonce()
    assert nonce.startswith("nonce-")
    assert mgr.verify_and_consume(nonce)
    assert not mgr.verify_and_consume(nonce)  # single-use only


def test_placeholder_detection():
    assert is_placeholder_value("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    assert is_placeholder_value("0000000000000000000000000000000000000000000000000000000000000000")
    assert is_placeholder_value("shared-contracts")
    assert is_placeholder_value("TODO")
    assert not is_placeholder_value("sha256:abc123def456")


def test_secret_redaction():
    text = "Key: nvapi-abcdef1234567890 and GHP: ghp_1234567890abcdef"
    redacted = redact_secrets(text)
    assert "nvapi-" not in redacted
    assert "ghp_" not in redacted
    assert "[REDACTED_SECRET]" in redacted


def test_claim_verification():
    att = create_attestation(
        execution_id="exec-456",
        executor_type="remote",
        command_argv=["bash", "deploy/server_up.sh"],
        exit_code=0,
        stdout_text="Deployed",
        stderr_text="",
        start_time_utc="2026-08-06T22:00:00Z",
        end_time_utc="2026-08-06T22:01:00Z",
        duration_s=60.0,
        image_id="brain-api:v0.5.4",
    )
    claim = ReportClaim(
        claim_id="c-1",
        claim_text="Deployed v0.5.4 to VPS",
        claim_category=ClaimCategory.DEPLOYMENT,
        required_evidence_types=[EvidenceAuthority.REMOTE_EXECUTOR],
        supplied_attestation_ids=[att.attestation_id],
    )
    status = verify_claim(claim, [att])
    assert status == EvidenceState.VERIFIED
    assert claim.verification_status == EvidenceState.VERIFIED
