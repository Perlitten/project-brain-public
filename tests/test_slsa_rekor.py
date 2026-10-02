"""Unit and Integration Tests for SLSA Provenance and Rekor Transparency Log."""

from brain.harness.attestation import create_attestation
from brain.harness.rekor import RekorClient
from brain.harness.slsa import generate_slsa_provenance


def test_slsa_provenance_generation():
    artifact_bytes = b"project-brain-v0.6.0-release-archive"
    slsa = generate_slsa_provenance("brain-api:v0.6.0", artifact_bytes)

    assert slsa.type == "https://in-toto.io/Statement/v0.1"
    assert slsa.subject[0].name == "brain-api:v0.6.0"
    assert "sha256" in slsa.subject[0].digest
    assert slsa.predicate["buildDefinition"]["externalParameters"]["revision"] == "96dba8285bd5e27a726715f5c3a3efd85c8eef86"


def test_rekor_transparency_log_submission_and_inclusion_verification(tmp_path):
    client = RekorClient(ledger_dir=tmp_path)
    att = create_attestation("exec-rekor-1", "remote", ["docker", "run"], 0, "ok", "", "t1", "t2", 1.0)

    entry = client.submit_attestation(att.attestation_hash, b"payload-bytes")
    assert entry.rekor_uuid.startswith("rekor-uuid-")
    assert entry.verified

    # Inclusion verification check
    assert client.verify_inclusion(entry.rekor_uuid, att.attestation_hash)
    assert not client.verify_inclusion(entry.rekor_uuid, "fake-hash")
    assert not client.verify_inclusion("fake-uuid", att.attestation_hash)
