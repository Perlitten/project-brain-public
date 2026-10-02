"""Evidence state lifecycle and model definitions for Project Brain v0.6.0 Trustworthy Harness."""

from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class EvidenceState(str, Enum):
    DECLARED = "DECLARED"
    PLANNED = "PLANNED"
    SCHEDULED = "SCHEDULED"
    STARTED = "STARTED"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    PARTIAL = "PARTIAL"
    SYNTHETIC = "SYNTHETIC"
    UNVERIFIABLE = "UNVERIFIABLE"
    SUPERSEDED = "SUPERSEDED"


class EvidenceAuthority(str, Enum):
    REMOTE_EXECUTOR = "remote_executor"
    LOCAL_EXECUTOR = "local_executor"
    EXTERNAL_HTTP_OBSERVER = "external_http_observer"
    CONTAINER_RUNTIME = "container_runtime"
    DATABASE_QUERY = "database_query"
    PROVIDER_RESPONSE = "provider_response"
    TEST_RUNNER = "test_runner"
    SYNTHETIC_FIXTURE = "synthetic_fixture"
    HUMAN_DECLARATION = "human_declaration"
    REPORT_GENERATOR = "report_generator"


class ClaimCategory(str, Enum):
    DEPLOYMENT = "deployment"
    BENCHMARK = "benchmark"
    OPERATIONS = "operations"
    SECURITY = "security"
    INTEGRITY = "integrity"


class V06AttestationPayload(BaseModel):
    """v0.6.0 Compact Attestation JSON Schema."""
    sub: str  # sha256 subject hash
    host: str
    nonce: str
    tree: str
    image: str
    ctr: str
    cmd: str
    exit: int
    out: str  # sha256 output hash
    state: EvidenceState = EvidenceState.EXECUTED
    sig: str  # HMAC-SHA256 signature
    rekor: Optional[str] = None  # Rekor transparency log UUID


class ReportClaim(BaseModel):
    claim_id: str
    claim_text: str
    claim_category: ClaimCategory
    required_evidence_types: List[EvidenceAuthority]
    supplied_attestation_ids: List[str] = Field(default_factory=list)
    verification_status: EvidenceState = EvidenceState.UNVERIFIABLE
    unresolved_evidence: List[str] = Field(default_factory=list)
    confidence: float = 0.0
    allowed_in_executive_summary: bool = False
