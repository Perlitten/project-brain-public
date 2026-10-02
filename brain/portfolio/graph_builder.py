"""Portfolio Graph Builder and Quality Gate.

Phases B4–B6: Cross-repository graph generation, quality validation,
and portfolio-level coupling metrics.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional, Set

from brain.portfolio.models import (
    ContractStatus,
    CrossRepoIdentity,
    CrossRepoRelationship,
    PortfolioGeneration,
    PortfolioGenStatus,
    PortfolioRecord,
)


class PortfolioGraphBuilder:
    """Builds portfolio-level cross-repository graphs.

    Lifecycle:
      resolve repository revisions
      → verify active repository graph generations
      → build inactive portfolio generation
      → extract cross-repository contracts
      → validate
      → activate atomically
      → retain prior generation
    """

    def __init__(self, portfolio: PortfolioRecord):
        self._portfolio = portfolio

    def build_generation(
        self,
        repository_revisions: Dict[str, str],
        repository_generations: Dict[str, str],
        declared_relationships: Optional[List[Dict[str, Any]]] = None,
    ) -> PortfolioGeneration:
        """Build a new portfolio graph generation from repository data."""
        gen_id = f"pgen-{uuid.uuid4().hex[:12]}"
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Build identities from declared data
        identities = self._extract_identities(repository_revisions)

        # Build relationships from contracts + declared relationships
        relationships = self._extract_relationships(
            repository_revisions, declared_relationships or []
        )

        # Classify contract status
        relationships = self._classify_contracts(relationships)

        generation = PortfolioGeneration(
            generation_id=gen_id,
            portfolio_id=self._portfolio.portfolio_id,
            status=PortfolioGenStatus.INACTIVE.value,
            repository_revisions=repository_revisions,
            repository_generations=repository_generations,
            identities=identities,
            relationships=relationships,
            created_at_utc=now,
        )

        # Run quality gate
        quality = PortfolioQualityGate.validate(generation, self._portfolio)
        generation.quality_report = quality

        return generation

    def activate(self, generation: PortfolioGeneration) -> PortfolioGeneration:
        """Activate a generation atomically."""
        if generation.quality_report.get("blocking_errors"):
            raise ValueError(
                f"Cannot activate generation with {len(generation.quality_report['blocking_errors'])} blocking errors"
            )

        generation.status = PortfolioGenStatus.ACTIVE.value
        generation.activated_at_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return generation

    def supersede(self, generation: PortfolioGeneration) -> PortfolioGeneration:
        """Mark a generation as superseded."""
        generation.status = PortfolioGenStatus.STALE.value
        generation.superseded_at_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return generation

    def _extract_identities(
        self, repository_revisions: Dict[str, str]
    ) -> List[CrossRepoIdentity]:
        """Extract portfolio-level identities from repository aliases."""
        identities = []
        for repo in self._portfolio.repositories:
            identity = CrossRepoIdentity(
                identity_type="repository",
                qualified_id=f"{self._portfolio.portfolio_id}:{repo.alias}",
                provider_repository=repo.repository_id,
                properties={"role": repo.role, "alias": repo.alias},
            )
            identities.append(identity)
        return identities

    def _extract_relationships(
        self,
        repository_revisions: Dict[str, str],
        declared_relationships: List[Dict[str, Any]],
    ) -> List[CrossRepoRelationship]:
        """Build relationships from contracts and declared data."""
        relationships = []

        # From declared contracts
        for contract in self._portfolio.contracts:
            src_repo_id = self._portfolio.get_repo_id(contract.from_repo)
            tgt_repo_id = self._portfolio.get_repo_id(contract.to_repo)
            if not src_repo_id or not tgt_repo_id:
                continue

            rel = CrossRepoRelationship(
                rel_type=contract.dep_type,
                source_repository=src_repo_id,
                target_repository=tgt_repo_id,
                source_entity=contract.from_repo,
                target_entity=contract.to_repo,
                evidence="declared_contract",
                extractor="portfolio_config",
                confidence="declared",
                source_revision=repository_revisions.get(src_repo_id, ""),
                target_revision=repository_revisions.get(tgt_repo_id, ""),
                contract_status=(
                    ContractStatus.ALLOWED.value if contract.allowed
                    else ContractStatus.FORBIDDEN.value
                ),
            )
            relationships.append(rel)

        # From declared relationships (additional detected ones)
        for decl in declared_relationships:
            rel = CrossRepoRelationship(
                rel_type=decl.get("rel_type", "DEPENDS_ON_PACKAGE"),
                source_repository=decl.get("source_repository", ""),
                target_repository=decl.get("target_repository", ""),
                source_entity=decl.get("source_entity", ""),
                target_entity=decl.get("target_entity", ""),
                evidence=decl.get("evidence", "declared"),
                extractor=decl.get("extractor", "manual"),
                confidence=decl.get("confidence", "inferred"),
                source_revision=decl.get("source_revision", ""),
                target_revision=decl.get("target_revision", ""),
                contract_status=decl.get("contract_status", ContractStatus.UNKNOWN.value),
            )
            relationships.append(rel)

        return relationships

    def _classify_contracts(
        self, relationships: List[CrossRepoRelationship]
    ) -> List[CrossRepoRelationship]:
        """Classify relationships against declared contracts."""
        contract_index = {}
        for c in self._portfolio.contracts:
            src_id = self._portfolio.get_repo_id(c.from_repo)
            tgt_id = self._portfolio.get_repo_id(c.to_repo)
            if src_id and tgt_id:
                key = (src_id, tgt_id, c.dep_type)
                contract_index[key] = c.allowed

        for rel in relationships:
            if rel.contract_status not in (ContractStatus.ALLOWED.value, ContractStatus.FORBIDDEN.value):
                key = (rel.source_repository, rel.target_repository, rel.rel_type)
                if key in contract_index:
                    rel.contract_status = (
                        ContractStatus.ALLOWED.value if contract_index[key]
                        else ContractStatus.FORBIDDEN.value
                    )
                else:
                    rel.contract_status = ContractStatus.UNDOCUMENTED.value

        return relationships


class PortfolioQualityGate:
    """Validates portfolio graph generation quality (Phase B5)."""

    @staticmethod
    def validate(
        generation: PortfolioGeneration,
        portfolio: PortfolioRecord,
    ) -> Dict[str, Any]:
        """Run quality checks. Returns report with blocking errors and warnings."""
        blocking = []
        warnings = []
        known_repos = {r.repository_id for r in portfolio.repositories}

        # Every edge must have source and target repository
        for rel in generation.relationships:
            if not rel.source_repository:
                blocking.append(f"Relationship missing source_repository: {rel.rel_type}")
            if not rel.target_repository:
                blocking.append(f"Relationship missing target_repository: {rel.rel_type}")

        # No unknown repository identities
        for rel in generation.relationships:
            if rel.source_repository and rel.source_repository not in known_repos:
                blocking.append(f"Unknown source repository: {rel.source_repository}")
            if rel.target_repository and rel.target_repository not in known_repos:
                blocking.append(f"Unknown target repository: {rel.target_repository}")

        # No duplicate relationship identity
        seen_fps = set()
        for rel in generation.relationships:
            fp = rel.fingerprint
            if fp in seen_fps:
                warnings.append(f"Duplicate relationship fingerprint: {fp}")
            seen_fps.add(fp)

        # No forbidden edge classified as allowed
        for rel in generation.relationships:
            if rel.contract_status == ContractStatus.FORBIDDEN.value:
                warnings.append(
                    f"Forbidden dependency: {rel.source_repository} -> {rel.target_repository} ({rel.rel_type})"
                )

        # Source revision should be present
        for repo_id in known_repos:
            if repo_id not in generation.repository_revisions:
                warnings.append(f"Missing revision for repository: {repo_id}")

        # Contract coverage
        undocumented = sum(
            1 for r in generation.relationships
            if r.contract_status == ContractStatus.UNDOCUMENTED.value
        )
        forbidden = sum(
            1 for r in generation.relationships
            if r.contract_status == ContractStatus.FORBIDDEN.value
        )

        return {
            "blocking_errors": blocking,
            "warnings": warnings,
            "total_relationships": len(generation.relationships),
            "total_identities": len(generation.identities),
            "undocumented_count": undocumented,
            "forbidden_count": forbidden,
            "stale_count": 0,
            "passed": len(blocking) == 0,
        }


class PortfolioCoupling:
    """Portfolio-level coupling metrics (Phase B6)."""

    @staticmethod
    def calculate(
        generation: PortfolioGeneration,
        portfolio: PortfolioRecord,
    ) -> Dict[str, Any]:
        """Calculate portfolio coupling metrics."""
        repo_ids = {r.repository_id for r in portfolio.repositories}

        # Fan-in / fan-out per repository
        fan_in: Dict[str, int] = {rid: 0 for rid in repo_ids}
        fan_out: Dict[str, int] = {rid: 0 for rid in repo_ids}

        for rel in generation.relationships:
            if rel.source_repository in fan_out:
                fan_out[rel.source_repository] += 1
            if rel.target_repository in fan_in:
                fan_in[rel.target_repository] += 1

        # Cross-repository instability = fan_out / (fan_in + fan_out)
        instability = {}
        for rid in repo_ids:
            total = fan_in[rid] + fan_out[rid]
            instability[rid] = fan_out[rid] / total if total > 0 else 0.0

        # Classify by type
        api_count = sum(1 for r in generation.relationships if "API" in r.rel_type)
        event_count = sum(1 for r in generation.relationships if "EVENT" in r.rel_type)
        package_count = sum(1 for r in generation.relationships if "PACKAGE" in r.rel_type)
        db_count = sum(1 for r in generation.relationships if "DATABASE" in r.rel_type or "SCHEMA" in r.rel_type)
        deploy_count = sum(1 for r in generation.relationships if "DEPLOY" in r.rel_type)

        undocumented = sum(
            1 for r in generation.relationships
            if r.contract_status == ContractStatus.UNDOCUMENTED.value
        )
        forbidden = sum(
            1 for r in generation.relationships
            if r.contract_status == ContractStatus.FORBIDDEN.value
        )

        # Circular repository dependencies (SCC)
        cycles = PortfolioCoupling._find_repo_cycles(generation, repo_ids)

        # Blast radius: max fan-out
        max_blast = max(fan_out.values()) if fan_out else 0

        return {
            "repository_fan_in": fan_in,
            "repository_fan_out": fan_out,
            "instability": instability,
            "api_coupling": api_count,
            "event_coupling": event_count,
            "package_coupling": package_count,
            "shared_database_coupling": db_count,
            "deployment_coupling": deploy_count,
            "undocumented_dependencies": undocumented,
            "forbidden_dependencies": forbidden,
            "circular_dependencies": cycles,
            "portfolio_blast_radius": max_blast,
            "total_relationships": len(generation.relationships),
        }

    @staticmethod
    def _find_repo_cycles(
        generation: PortfolioGeneration,
        repo_ids: Set[str],
    ) -> List[List[str]]:
        """Find circular repository dependencies using iterative DFS."""
        adj: Dict[str, Set[str]] = {rid: set() for rid in repo_ids}
        for rel in generation.relationships:
            if rel.source_repository in adj and rel.target_repository in adj:
                if rel.source_repository != rel.target_repository:
                    adj[rel.source_repository].add(rel.target_repository)

        # Tarjan's SCC
        index_counter = [0]
        stack: List[str] = []
        on_stack: Set[str] = set()
        indices: Dict[str, int] = {}
        lowlinks: Dict[str, int] = {}
        sccs: List[List[str]] = []

        def strongconnect(v: str):
            indices[v] = lowlinks[v] = index_counter[0]
            index_counter[0] += 1
            stack.append(v)
            on_stack.add(v)

            for w in adj.get(v, set()):
                if w not in indices:
                    strongconnect(w)
                    lowlinks[v] = min(lowlinks[v], lowlinks[w])
                elif w in on_stack:
                    lowlinks[v] = min(lowlinks[v], indices[w])

            if lowlinks[v] == indices[v]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == v:
                        break
                if len(scc) > 1:
                    sccs.append(scc)

        for v in repo_ids:
            if v not in indices:
                strongconnect(v)

        return sccs
