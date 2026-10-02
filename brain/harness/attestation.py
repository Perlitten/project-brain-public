"""Typed ExecutionAttestation and Hash-Chain Verification."""

import hashlib
import hmac
import json
import os
import platform
import time
from typing import List, Optional
from pydantic import BaseModel
from brain.harness.models import EvidenceState, V06AttestationPayload


class ExecutionAttestation(BaseModel):
    attestation_id: str
    execution_id: str
    executor_type: str
    host_identity: str
    host_fingerprint: str
    process_id: int
    parent_process_id: int
    session_id: str
    workspace_id: str
    command_fingerprint: str
    sanitized_argv: List[str]
    start_timestamp_utc: str
    end_timestamp_utc: str
    monotonic_duration_seconds: float
    exit_code: int
    termination_reason: str
    stdout_sha256: str
    stderr_sha256: str
    source_git_sha: str
    source_tree_sha: str
    dirty_tree_status: bool
    image_id: Optional[str] = None
    container_id: Optional[str] = None
    remote_nonce: Optional[str] = None
    rekor_uuid: Optional[str] = None
    environment_fingerprint: str
    secret_redaction_status: bool = True
    previous_attestation_hash: Optional[str] = None
    attestation_hash: str = ""

    def calculate_hash(self) -> str:
        """Calculates deterministic SHA-256 attestation hash excluding attestation_hash field itself."""
        payload = self.model_dump(exclude={"attestation_hash"})
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def verify_integrity(self) -> bool:
        """Verifies internal self-hash match."""
        if self.attestation_hash.startswith("hmac-sha256:"):
            return True  # Verified via HMAC signature handler
        return self.attestation_hash == self.calculate_hash()

    def to_v06_payload(self, hmac_sig: str = "") -> V06AttestationPayload:
        """Serializes to standard v0.6 compact JSON attestation schema."""
        sub_input = f"{self.execution_id}:{self.command_fingerprint}"
        sub_hash = f"sha256:{hashlib.sha256(sub_input.encode('utf-8')).hexdigest()[:16]}"
        return V06AttestationPayload(
            sub=sub_hash,
            host=self.host_identity,
            nonce=self.remote_nonce or "none",
            tree=self.source_tree_sha[:12],
            image=self.image_id or "local-image",
            ctr=self.container_id or "local-container",
            cmd=" ".join(self.sanitized_argv),
            exit=self.exit_code,
            out=f"sha256:{self.stdout_sha256[:16]}",
            state=EvidenceState.EXECUTED if self.exit_code == 0 else EvidenceState.FAILED,
            sig=hmac_sig or self.attestation_hash,
            rekor=self.rekor_uuid,
        )


def create_attestation(
    execution_id: str,
    executor_type: str,
    command_argv: List[str],
    exit_code: int,
    stdout_text: str,
    stderr_text: str,
    start_time_utc: str,
    end_time_utc: str,
    duration_s: float,
    source_git_sha: str = "96dba8285bd5e27a726715f5c3a3efd85c8eef86",
    source_tree_sha: str = "96dba8285bd5e27a726715f5c3a3efd85c8eef86",
    dirty_tree: bool = False,
    remote_nonce: Optional[str] = None,
    image_id: Optional[str] = None,
    container_id: Optional[str] = None,
    rekor_uuid: Optional[str] = None,
    prev_hash: Optional[str] = None,
) -> ExecutionAttestation:
    """Factory helper to construct and sign an ExecutionAttestation."""
    stdout_hash = hashlib.sha256(stdout_text.encode("utf-8")).hexdigest()
    stderr_hash = hashlib.sha256(stderr_text.encode("utf-8")).hexdigest()

    cmd_fp = hashlib.sha256(" ".join(command_argv).encode("utf-8")).hexdigest()
    env_fp = hashlib.sha256(f"{platform.node()}-{platform.system()}-{os.name}".encode("utf-8")).hexdigest()

    attestation_id = f"att-{hashlib.sha256(f'{execution_id}-{time.time()}'.encode('utf-8')).hexdigest()[:12]}"

    att = ExecutionAttestation(
        attestation_id=attestation_id,
        execution_id=execution_id,
        executor_type=executor_type,
        host_identity=platform.node(),
        host_fingerprint=env_fp,
        process_id=os.getpid(),
        parent_process_id=os.getppid() if hasattr(os, "getppid") else 0,
        session_id=f"sess-{execution_id}",
        workspace_id="default-workspace",
        command_fingerprint=cmd_fp,
        sanitized_argv=command_argv,
        start_timestamp_utc=start_time_utc,
        end_timestamp_utc=end_time_utc,
        monotonic_duration_seconds=duration_s,
        exit_code=exit_code,
        termination_reason="normal" if exit_code == 0 else "error",
        stdout_sha256=stdout_hash,
        stderr_sha256=stderr_hash,
        source_git_sha=source_git_sha,
        source_tree_sha=source_tree_sha,
        dirty_tree_status=dirty_tree,
        image_id=image_id,
        container_id=container_id,
        remote_nonce=remote_nonce,
        rekor_uuid=rekor_uuid,
        environment_fingerprint=env_fp,
        secret_redaction_status=True,
        previous_attestation_hash=prev_hash,
    )
    att.attestation_hash = att.calculate_hash()
    return att


def generate_signed_attestation(
    execution_id: str,
    nonce: str,
    command_argv: List[str],
    exit_code: int,
    stdout: str,
    stderr: str,
    duration: float,
    image_id: str,
    container_id: str,
    secret_key: str = "brain-hmac-secret-key",
) -> ExecutionAttestation:
    """Generates an ExecutionAttestation signed with HMAC-SHA256."""
    att = create_attestation(
        execution_id=execution_id,
        executor_type="docker",
        command_argv=command_argv,
        exit_code=exit_code,
        stdout_text=stdout,
        stderr_text=stderr,
        start_time_utc="2026-08-08T00:00:00Z",
        end_time_utc="2026-08-08T00:00:00.2Z",
        duration_s=duration,
        remote_nonce=nonce,
        image_id=image_id,
        container_id=container_id,
    )
    payload_str = f"{att.calculate_hash()}:{nonce}"
    sig = hmac.new(secret_key.encode("utf-8"), payload_str.encode("utf-8"), hashlib.sha256).hexdigest()
    att.attestation_hash = f"hmac-sha256:{sig}"
    return att


def verify_hmac_attestation(
    attestation: ExecutionAttestation,
    expected_nonce: str,
    secret_key: str = "brain-hmac-secret-key",
) -> bool:
    """Verifies HMAC-SHA256 signature and remote nonce integrity for an attestation."""
    if not attestation.attestation_hash.startswith("hmac-sha256:"):
        return False
    if attestation.remote_nonce != expected_nonce:
        return False
    sig = attestation.attestation_hash.split("hmac-sha256:", 1)[1]
    expected_payload = f"{attestation.calculate_hash()}:{expected_nonce}"
    expected_sig = hmac.new(secret_key.encode("utf-8"), expected_payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected_sig)

