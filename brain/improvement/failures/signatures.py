"""Deterministic Failure Signature Extractor."""

import hashlib
from brain.improvement.models import TrajectoryRecord


def extract_deterministic_signature(record: TrajectoryRecord) -> str:
    """Computes stable deterministic signature hash from test errors, exceptions, and failure modes."""
    failures = sorted(record.outcome.failure_modes)
    if not failures:
        return "clean_success"

    raw_signature = f"{record.task.category}:{':'.join(failures)}"
    sig_hash = hashlib.sha256(raw_signature.encode("utf-8")).hexdigest()[:12]
    return f"sig-{record.task.category}-{sig_hash}"
