"""Automated test for HMAC cryptographic remote observe attestation and tamper detection."""

from brain.harness.nonce import RemoteNonceManager
from brain.harness.attestation import generate_signed_attestation, verify_hmac_attestation


def test_remote_observe_hmac_signature_and_tamper_detection():
    nonce_mgr = RemoteNonceManager(ttl_seconds=60)
    nonce = nonce_mgr.generate_nonce()

    att = generate_signed_attestation(
        execution_id="test-exec-1",
        nonce=nonce,
        command_argv=["docker", "ps"],
        exit_code=0,
        stdout="container-ok",
        stderr="",
        duration=0.2,
        image_id="brain-api:96dba8285bd5e27a726715f5c3a3efd85c8eef86",
        container_id="brain-api-ct",
    )

    # 1. Verify valid signature and nonce consumption
    assert verify_hmac_attestation(att, nonce)
    assert nonce_mgr.verify_and_consume(nonce)
    assert not nonce_mgr.verify_and_consume(nonce)  # Replay attack prevented

    # 2. Tamper test: Modify 1 byte in stdout_sha256
    tampered = att.model_copy(deep=True)
    tampered.stdout_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"

    # Must fail closed
    assert not verify_hmac_attestation(tampered, nonce)
