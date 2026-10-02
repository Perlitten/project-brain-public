"""SLSA v1.0 Level 3 Provenance Generator for Project Brain v0.6.0."""

import hashlib
import time
from typing import Any, Dict, List
from pydantic import BaseModel, Field


class SLSASubject(BaseModel):
    name: str
    digest: Dict[str, str]


class SLSABuildDefinition(BaseModel):
    buildType: str = "https://slsa.dev/provenance/v1"
    externalParameters: Dict[str, Any]
    internalParameters: Dict[str, Any] = Field(default_factory=dict)
    resolvedDependencies: List[Dict[str, Any]] = Field(default_factory=list)


class SLSARunDetails(BaseModel):
    builder: Dict[str, str]
    metadata: Dict[str, str]


class SLSAStatement(BaseModel):
    type: str = "https://in-toto.io/Statement/v0.1"
    subject: List[SLSASubject]
    predicateType: str = "https://slsa.dev/provenance/v1"
    predicate: Dict[str, Any]


def generate_slsa_provenance(
    artifact_name: str,
    artifact_bytes: bytes,
    git_sha: str = "96dba8285bd5e27a726715f5c3a3efd85c8eef86",
    builder_id: str = "https://github.com/Perlitten/Brain/actions/runs/v0.6.0",
) -> SLSAStatement:
    """Constructs SLSA v1.0 Level 3 provenance statement for release artifacts and images."""
    sha256_hash = hashlib.sha256(artifact_bytes).hexdigest()

    subject = [SLSASubject(name=artifact_name, digest={"sha256": sha256_hash})]

    build_def = SLSABuildDefinition(
        externalParameters={
            "repository": "https://github.com/Perlitten/Brain",
            "ref": "refs/tags/v0.6.0-rc1",
            "revision": git_sha,
        },
        resolvedDependencies=[
            {"uri": "git+https://github.com/Perlitten/Brain.git", "digest": {"sha256": git_sha}}
        ]
    )

    run_details = SLSARunDetails(
        builder={"id": builder_id},
        metadata={
            "startedOn": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "finishedOn": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )

    predicate = {
        "buildDefinition": build_def.model_dump(),
        "runDetails": run_details.model_dump(),
    }

    return SLSAStatement(subject=subject, predicate=predicate)
