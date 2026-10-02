"""Fail-Closed Evidence Verifier and Placeholder Detection Engine for v0.6.0."""

import re
from typing import Any, List
from brain.harness.attestation import ExecutionAttestation
from brain.harness.models import EvidenceState, ReportClaim

# Known fake / placeholder SHA-256 hashes
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ALL_ZERO_SHA256 = "0000000000000000000000000000000000000000000000000000000000000000"

# Forbidden fixture repos and placeholder indicators
FORBIDDEN_INDICATORS = {
    "", "unknown", "todo", "placeholder", "null", "none", "0/0", "gen-0", "unknown_rev"
}
FIXTURE_REPOS = {"shared-contracts", "api-service", "worker-pool"}

# Secret redaction patterns
SECRET_PATTERNS = [
    (re.compile(r"nvapi-[A-Za-z0-9_\-]+"), "[REDACTED_SECRET]"),
    (re.compile(r"ghp_[A-Za-z0-9_\-]+"), "[REDACTED_SECRET]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]+"), "[REDACTED_SECRET]"),
    (re.compile(r"POSTGRES_PASSWORD=[^\s]+"), "POSTGRES_PASSWORD=[REDACTED_SECRET]"),
]


def redact_secrets(text: str) -> str:
    """Redacts API keys and sensitive tokens from log text."""
    redacted = text
    for pat, repl in SECRET_PATTERNS:
        redacted = pat.sub(repl, redacted)
    return redacted


def is_placeholder_value(val: Any) -> bool:
    """Detects fake, synthetic placeholder, traversal, or unexecuted values."""
    if val is None:
        return True
    if isinstance(val, str):
        v = val.strip().lower()
        if v in FORBIDDEN_INDICATORS:
            return True
        if v == EMPTY_SHA256 or v == ALL_ZERO_SHA256:
            return True
        if v in FIXTURE_REPOS:
            return True
        if ".." in v or v.startswith("/etc/") or v.startswith("/var/"):
            return True
    return False


def verify_claim(claim: ReportClaim, attestations: List[ExecutionAttestation]) -> EvidenceState:
    """Fail-closed evaluation of a report claim against provided attestations."""
    if not claim.supplied_attestation_ids or not attestations:
        return EvidenceState.UNVERIFIABLE

    att_map = {att.attestation_id: att for att in attestations}
    for att_id in claim.supplied_attestation_ids:
        att = att_map.get(att_id)
        if not att:
            return EvidenceState.UNVERIFIABLE
        if not att.verify_integrity():
            return EvidenceState.FAILED
        if att.exit_code != 0:
            return EvidenceState.FAILED
        if is_placeholder_value(att.image_id) or is_placeholder_value(att.stdout_sha256):
            return EvidenceState.SYNTHETIC

    claim.verification_status = EvidenceState.VERIFIED
    claim.confidence = 1.0
    claim.allowed_in_executive_summary = True
    return EvidenceState.VERIFIED
