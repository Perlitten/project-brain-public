"""Portfolio and Cross-Repository Graph Models.

Phases B1–B3: Portfolio records, cross-repository identities,
and conservative cross-repository relationships.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ContractStatus(str, Enum):
    ALLOWED = "allowed"
    FORBIDDEN = "forbidden"
    UNDOCUMENTED = "undocumented"
    UNKNOWN = "unknown"


class CrossRepoRelType(str, Enum):
    CALLS_API = "CALLS_API"
    PROVIDES_API = "PROVIDES_API"
    PRODUCES_EVENT = "PRODUCES_EVENT"
    CONSUMES_EVENT = "CONSUMES_EVENT"
    DEPENDS_ON_PACKAGE = "DEPENDS_ON_PACKAGE"
    PROVIDES_PACKAGE = "PROVIDES_PACKAGE"
    SHARES_DATABASE = "SHARES_DATABASE"
    READS_SCHEMA = "READS_SCHEMA"
    WRITES_SCHEMA = "WRITES_SCHEMA"
    USES_CONFIGURATION = "USES_CONFIGURATION"
    GENERATES_CLIENT = "GENERATES_CLIENT"
    DEPLOYS_WITH = "DEPLOYS_WITH"
    MIGRATES_WITH = "MIGRATES_WITH"


class PortfolioGenStatus(str, Enum):
    INACTIVE = "inactive"
    BUILDING = "building"
    ACTIVE = "active"
    STALE = "stale"
    FAILED = "failed"


# ── Portfolio Record ──

@dataclass
class DependencyContract:
    """Declared dependency contract between repositories in a portfolio."""
    from_repo: str  # Repository alias in portfolio
    to_repo: str
    dep_type: str  # CrossRepoRelType value
    allowed: bool
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PortfolioRepository:
    """A repository's role within a portfolio."""
    repository_id: str
    alias: str
    role: str
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PortfolioRecord:
    """Multi-repository portfolio configuration."""
    portfolio_id: str
    display_name: str
    repositories: List[PortfolioRepository] = field(default_factory=list)
    owners: List[str] = field(default_factory=list)
    architecture_policy: str = ""
    contracts: List[DependencyContract] = field(default_factory=list)
    trust_constraints: Dict[str, str] = field(default_factory=dict)
    active_generation_id: Optional[str] = None
    schema_version: int = 1
    created_at_utc: str = ""
    updated_at_utc: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> PortfolioRecord:
        repos = [PortfolioRepository(**r) for r in d.get("repositories", [])]
        contracts = [DependencyContract(**c) for c in d.get("contracts", [])]
        return cls(
            portfolio_id=d["portfolio_id"],
            display_name=d["display_name"],
            repositories=repos,
            owners=d.get("owners", []),
            architecture_policy=d.get("architecture_policy", ""),
            contracts=contracts,
            trust_constraints=d.get("trust_constraints", {}),
            active_generation_id=d.get("active_generation_id"),
            schema_version=d.get("schema_version", 1),
            created_at_utc=d.get("created_at_utc", ""),
            updated_at_utc=d.get("updated_at_utc", ""),
        )

    def get_repo_aliases(self) -> List[str]:
        return [r.alias for r in self.repositories]

    def get_repo_id(self, alias: str) -> Optional[str]:
        for r in self.repositories:
            if r.alias == alias:
                return r.repository_id
        return None


# ── Cross-Repository Identity ──

@dataclass
class CrossRepoIdentity:
    """Portfolio-level identity for cross-repository entities."""
    identity_type: str  # 'package', 'api_endpoint', 'event_topic', etc.
    qualified_id: str
    provider_repository: str
    ecosystem: str = ""
    api_namespace: str = ""
    http_method: str = ""
    normalized_route: str = ""
    contract_version: str = ""
    package_name: str = ""
    package_version: str = ""
    event_namespace: str = ""
    topic: str = ""
    schema_version: str = ""
    properties: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Cross-Repository Relationship ──

@dataclass
class CrossRepoRelationship:
    """Conservative cross-repository relationship with evidence."""
    rel_type: str  # CrossRepoRelType value
    source_repository: str
    target_repository: str
    source_entity: str
    target_entity: str
    evidence: str
    extractor: str
    confidence: str  # 'exact', 'inferred', 'declared'
    source_revision: str
    target_revision: str = ""
    contract_status: str = ContractStatus.UNKNOWN.value
    properties: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def fingerprint(self) -> str:
        content = f"{self.rel_type}:{self.source_repository}:{self.target_repository}:{self.source_entity}:{self.target_entity}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]


# ── Portfolio Graph Generation ──

@dataclass
class PortfolioGeneration:
    """A versioned portfolio graph generation."""
    generation_id: str
    portfolio_id: str
    status: str = PortfolioGenStatus.INACTIVE.value
    repository_revisions: Dict[str, str] = field(default_factory=dict)
    repository_generations: Dict[str, str] = field(default_factory=dict)
    identities: List[CrossRepoIdentity] = field(default_factory=list)
    relationships: List[CrossRepoRelationship] = field(default_factory=list)
    quality_report: Dict[str, Any] = field(default_factory=dict)
    created_at_utc: str = ""
    activated_at_utc: str = ""
    superseded_at_utc: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> PortfolioGeneration:
        identities = [CrossRepoIdentity(**i) for i in d.get("identities", [])]
        relationships = [CrossRepoRelationship(**r) for r in d.get("relationships", [])]
        return cls(
            generation_id=d["generation_id"],
            portfolio_id=d["portfolio_id"],
            status=d.get("status", PortfolioGenStatus.INACTIVE.value),
            repository_revisions=d.get("repository_revisions", {}),
            repository_generations=d.get("repository_generations", {}),
            identities=identities,
            relationships=relationships,
            quality_report=d.get("quality_report", {}),
            created_at_utc=d.get("created_at_utc", ""),
            activated_at_utc=d.get("activated_at_utc", ""),
            superseded_at_utc=d.get("superseded_at_utc", ""),
        )


# ── Portfolio Configuration Parser ──

def parse_portfolio_config(config: Dict[str, Any]) -> PortfolioRecord:
    """Parse a portfolio YAML/dict configuration into a PortfolioRecord.

    Validates: duplicate aliases, unknown repo IDs (deferred),
    circular portfolio inclusion, invalid dep types, contradictory contracts,
    missing owners, unsupported schema versions.
    """
    schema_version = config.get("version", 1)
    if schema_version not in (1,):
        raise ValueError(f"Unsupported portfolio schema version: {schema_version}")

    portfolio_cfg = config.get("portfolio", {})
    name = portfolio_cfg.get("name", "unnamed")

    # Parse repositories
    repos_cfg = config.get("repositories", {})
    repos = []
    aliases_seen = set()
    for alias, rcfg in repos_cfg.items():
        if alias in aliases_seen:
            raise ValueError(f"Duplicate repository alias: '{alias}'")
        aliases_seen.add(alias)
        repos.append(PortfolioRepository(
            repository_id=rcfg.get("repository_id", alias),
            alias=alias,
            role=rcfg.get("role", ""),
            description=rcfg.get("description", ""),
        ))

    # Parse contracts
    contracts_cfg = config.get("contracts", [])
    contracts = []
    for cc in contracts_cfg:
        from_alias = cc.get("from", "")
        to_alias = cc.get("to", "")
        if from_alias not in aliases_seen:
            raise ValueError(f"Contract references unknown repository alias: '{from_alias}'")
        if to_alias not in aliases_seen:
            raise ValueError(f"Contract references unknown repository alias: '{to_alias}'")

        dep_type = cc.get("type", "source_import")
        allowed = cc.get("allowed", False)
        contracts.append(DependencyContract(
            from_repo=from_alias,
            to_repo=to_alias,
            dep_type=dep_type,
            allowed=allowed,
            description=cc.get("description", ""),
        ))

    # Check for circular self-references
    for c in contracts:
        if c.from_repo == c.to_repo:
            raise ValueError(f"Self-referencing contract: '{c.from_repo}' -> '{c.to_repo}'")

    # Check contradictory contracts (same from/to/type with different allowed)
    seen_contracts: dict[tuple[str, str, str], bool] = {}
    for c in contracts:
        key = (c.from_repo, c.to_repo, c.dep_type)
        if key in seen_contracts:
            if seen_contracts[key] != c.allowed:
                raise ValueError(
                    f"Contradictory contracts for {c.from_repo} -> {c.to_repo} ({c.dep_type})"
                )
        seen_contracts[key] = c.allowed

    portfolio_id = f"portfolio-{hashlib.sha256(name.encode()).hexdigest()[:10]}"

    return PortfolioRecord(
        portfolio_id=portfolio_id,
        display_name=name,
        repositories=repos,
        owners=portfolio_cfg.get("owners", []),
        architecture_policy=portfolio_cfg.get("architecture_policy", ""),
        contracts=contracts,
        schema_version=schema_version,
    )
