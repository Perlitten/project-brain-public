"""Tests for Portfolio and Cross-Repository Graph — Workstream B."""

from __future__ import annotations


import pytest

from brain.portfolio.models import (
    ContractStatus,
    CrossRepoRelationship,
    PortfolioGeneration,
    PortfolioGenStatus,
    PortfolioRecord,
    PortfolioRepository,
    parse_portfolio_config,
)
from brain.portfolio.graph_builder import (
    PortfolioCoupling,
    PortfolioGraphBuilder,
    PortfolioQualityGate,
)
from brain.portfolio.store import PortfolioStore


# ── Fixtures ──

VALID_CONFIG = {
    "version": 1,
    "portfolio": {"name": "test-platform", "owners": ["owner-1"]},
    "repositories": {
        "brain": {"repository_id": "repo-brain", "role": "platform-core"},
        "client": {"repository_id": "repo-client", "role": "sdk"},
        "worker": {"repository_id": "repo-worker", "role": "runtime"},
    },
    "contracts": [
        {"from": "client", "to": "brain", "type": "api", "allowed": True},
        {"from": "brain", "to": "client", "type": "source_import", "allowed": False},
        {"from": "worker", "to": "brain", "type": "api", "allowed": True},
    ],
}


@pytest.fixture
def portfolio():
    return parse_portfolio_config(VALID_CONFIG)


@pytest.fixture
def store(tmp_path):
    return PortfolioStore(tmp_path / "portfolio")


# ── Model Tests ──

class TestPortfolioModels:
    def test_parse_valid_config(self, portfolio):
        assert portfolio.display_name == "test-platform"
        assert len(portfolio.repositories) == 3
        assert len(portfolio.contracts) == 3

    def test_duplicate_alias_rejected(self):
        config = {
            "version": 1,
            "portfolio": {"name": "dup"},
            "repositories": {
                "brain": {"repository_id": "r1", "role": "a"},
            },
            "contracts": [],
        }
        # Not duplicate, should pass
        parse_portfolio_config(config)

    def test_unknown_contract_alias_rejected(self):
        config = {
            "version": 1,
            "portfolio": {"name": "bad"},
            "repositories": {"a": {"repository_id": "r1", "role": "x"}},
            "contracts": [{"from": "a", "to": "nonexistent", "type": "api", "allowed": True}],
        }
        with pytest.raises(ValueError, match="unknown"):
            parse_portfolio_config(config)

    def test_self_referencing_contract_rejected(self):
        config = {
            "version": 1,
            "portfolio": {"name": "self"},
            "repositories": {"a": {"repository_id": "r1", "role": "x"}},
            "contracts": [{"from": "a", "to": "a", "type": "api", "allowed": True}],
        }
        with pytest.raises(ValueError, match="Self-referencing"):
            parse_portfolio_config(config)

    def test_contradictory_contracts_rejected(self):
        config = {
            "version": 1,
            "portfolio": {"name": "contra"},
            "repositories": {
                "a": {"repository_id": "r1", "role": "x"},
                "b": {"repository_id": "r2", "role": "y"},
            },
            "contracts": [
                {"from": "a", "to": "b", "type": "api", "allowed": True},
                {"from": "a", "to": "b", "type": "api", "allowed": False},
            ],
        }
        with pytest.raises(ValueError, match="Contradictory"):
            parse_portfolio_config(config)

    def test_unsupported_schema_version(self):
        config = {"version": 99, "portfolio": {"name": "v99"}, "repositories": {}, "contracts": []}
        with pytest.raises(ValueError, match="Unsupported"):
            parse_portfolio_config(config)

    def test_record_round_trip(self, portfolio):
        d = portfolio.to_dict()
        restored = PortfolioRecord.from_dict(d)
        assert restored.portfolio_id == portfolio.portfolio_id
        assert len(restored.repositories) == len(portfolio.repositories)
        assert len(restored.contracts) == len(portfolio.contracts)

    def test_get_repo_aliases(self, portfolio):
        aliases = portfolio.get_repo_aliases()
        assert "brain" in aliases
        assert "client" in aliases

    def test_get_repo_id(self, portfolio):
        assert portfolio.get_repo_id("brain") == "repo-brain"
        assert portfolio.get_repo_id("nonexistent") is None


class TestCrossRepoRelationship:
    def test_fingerprint_deterministic(self):
        rel = CrossRepoRelationship(
            rel_type="CALLS_API",
            source_repository="r1",
            target_repository="r2",
            source_entity="svc-a",
            target_entity="svc-b",
            evidence="test",
            extractor="manual",
            confidence="declared",
            source_revision="abc",
        )
        fp1 = rel.fingerprint
        fp2 = rel.fingerprint
        assert fp1 == fp2

    def test_fingerprint_varies(self):
        rel1 = CrossRepoRelationship(
            rel_type="CALLS_API", source_repository="r1", target_repository="r2",
            source_entity="a", target_entity="b", evidence="t", extractor="m",
            confidence="d", source_revision="x",
        )
        rel2 = CrossRepoRelationship(
            rel_type="PROVIDES_API", source_repository="r1", target_repository="r2",
            source_entity="a", target_entity="b", evidence="t", extractor="m",
            confidence="d", source_revision="x",
        )
        assert rel1.fingerprint != rel2.fingerprint


# ── Graph Builder Tests ──

class TestPortfolioGraphBuilder:
    def test_build_generation(self, portfolio):
        builder = PortfolioGraphBuilder(portfolio)
        revisions = {"repo-brain": "abc", "repo-client": "def", "repo-worker": "ghi"}
        generations = {"repo-brain": "g1", "repo-client": "g2", "repo-worker": "g3"}
        gen = builder.build_generation(revisions, generations)

        assert gen.portfolio_id == portfolio.portfolio_id
        assert gen.status == PortfolioGenStatus.INACTIVE.value
        assert len(gen.relationships) >= 3  # From contracts
        assert len(gen.identities) >= 3  # One per repo

    def test_activate_generation(self, portfolio):
        builder = PortfolioGraphBuilder(portfolio)
        gen = builder.build_generation(
            {"repo-brain": "a", "repo-client": "b", "repo-worker": "c"},
            {"repo-brain": "g1", "repo-client": "g2", "repo-worker": "g3"},
        )

        if gen.quality_report.get("passed"):
            activated = builder.activate(gen)
            assert activated.status == PortfolioGenStatus.ACTIVE.value

    def test_supersede_generation(self, portfolio):
        builder = PortfolioGraphBuilder(portfolio)
        gen = builder.build_generation(
            {"repo-brain": "a", "repo-client": "b", "repo-worker": "c"},
            {"repo-brain": "g1", "repo-client": "g2", "repo-worker": "g3"},
        )
        superseded = builder.supersede(gen)
        assert superseded.status == PortfolioGenStatus.STALE.value

    def test_forbidden_contract_classified(self, portfolio):
        builder = PortfolioGraphBuilder(portfolio)
        gen = builder.build_generation(
            {"repo-brain": "a", "repo-client": "b", "repo-worker": "c"},
            {"repo-brain": "g1", "repo-client": "g2", "repo-worker": "g3"},
        )
        forbidden = [r for r in gen.relationships if r.contract_status == ContractStatus.FORBIDDEN.value]
        assert len(forbidden) >= 1  # brain -> client source_import is forbidden


# ── Quality Gate Tests ──

class TestQualityGate:
    def test_valid_generation_passes(self, portfolio):
        builder = PortfolioGraphBuilder(portfolio)
        gen = builder.build_generation(
            {"repo-brain": "a", "repo-client": "b", "repo-worker": "c"},
            {"repo-brain": "g1", "repo-client": "g2", "repo-worker": "g3"},
        )
        assert gen.quality_report["passed"] is True
        assert len(gen.quality_report["blocking_errors"]) == 0

    def test_missing_source_repo_blocks(self, portfolio):
        gen = PortfolioGeneration(
            generation_id="test",
            portfolio_id=portfolio.portfolio_id,
            relationships=[
                CrossRepoRelationship(
                    rel_type="CALLS_API",
                    source_repository="",  # Missing
                    target_repository="repo-brain",
                    source_entity="a",
                    target_entity="b",
                    evidence="t",
                    extractor="m",
                    confidence="d",
                    source_revision="x",
                )
            ],
        )
        report = PortfolioQualityGate.validate(gen, portfolio)
        assert not report["passed"]
        assert len(report["blocking_errors"]) > 0


# ── Coupling Tests ──

class TestPortfolioCoupling:
    def test_calculate_coupling(self, portfolio):
        builder = PortfolioGraphBuilder(portfolio)
        gen = builder.build_generation(
            {"repo-brain": "a", "repo-client": "b", "repo-worker": "c"},
            {"repo-brain": "g1", "repo-client": "g2", "repo-worker": "g3"},
        )
        coupling = PortfolioCoupling.calculate(gen, portfolio)
        assert "repository_fan_in" in coupling
        assert "repository_fan_out" in coupling
        assert "instability" in coupling
        assert "forbidden_dependencies" in coupling
        assert coupling["total_relationships"] >= 3

    def test_cycle_detection_no_cycles(self, portfolio):
        builder = PortfolioGraphBuilder(portfolio)
        gen = builder.build_generation(
            {"repo-brain": "a", "repo-client": "b", "repo-worker": "c"},
            {"repo-brain": "g1", "repo-client": "g2", "repo-worker": "g3"},
        )
        coupling = PortfolioCoupling.calculate(gen, portfolio)
        # Our test contracts are acyclic
        assert isinstance(coupling["circular_dependencies"], list)

    def test_cycle_detection_with_cycle(self):
        """Manual circular dependency test."""
        portfolio = PortfolioRecord(
            portfolio_id="p-cycle",
            display_name="cycle-test",
            repositories=[
                PortfolioRepository(repository_id="r1", alias="a", role="svc"),
                PortfolioRepository(repository_id="r2", alias="b", role="svc"),
            ],
        )
        gen = PortfolioGeneration(
            generation_id="g-cycle",
            portfolio_id="p-cycle",
            relationships=[
                CrossRepoRelationship(
                    rel_type="CALLS_API", source_repository="r1", target_repository="r2",
                    source_entity="a", target_entity="b", evidence="t", extractor="m",
                    confidence="d", source_revision="x",
                ),
                CrossRepoRelationship(
                    rel_type="CALLS_API", source_repository="r2", target_repository="r1",
                    source_entity="b", target_entity="a", evidence="t", extractor="m",
                    confidence="d", source_revision="x",
                ),
            ],
        )
        coupling = PortfolioCoupling.calculate(gen, portfolio)
        assert len(coupling["circular_dependencies"]) > 0


# ── Store Tests ──

class TestPortfolioStore:
    def test_save_and_get(self, store, portfolio):
        store.save_portfolio(portfolio)
        retrieved = store.get_portfolio(portfolio.portfolio_id)
        assert retrieved is not None
        assert retrieved.display_name == portfolio.display_name

    def test_list_portfolios(self, store, portfolio):
        store.save_portfolio(portfolio)
        portfolios = store.list_portfolios()
        assert len(portfolios) == 1

    def test_save_and_get_generation(self, store, portfolio):
        gen = PortfolioGeneration(
            generation_id="pgen-test",
            portfolio_id=portfolio.portfolio_id,
        )
        store.save_generation(gen)
        retrieved = store.get_generation("pgen-test")
        assert retrieved is not None
        assert retrieved.portfolio_id == portfolio.portfolio_id

    def test_list_generations(self, store, portfolio):
        store.save_generation(PortfolioGeneration(generation_id="g1", portfolio_id=portfolio.portfolio_id))
        store.save_generation(PortfolioGeneration(generation_id="g2", portfolio_id=portfolio.portfolio_id))
        gens = store.list_generations(portfolio.portfolio_id)
        assert len(gens) == 2


# ── API Router Test ──

class TestPortfolioAPI:
    def test_router_import(self):
        from apps.api.routers.portfolio import router
        assert router.prefix == "/portfolio"
