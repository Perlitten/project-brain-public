"""A real five-repository fixture portfolio with real Git history.

The v0.5.0 scenarios are about what happens between repositories — a schema
change in one, a stale graph in another, a forbidden import across the
boundary. Mocking Git or the filesystem would hide exactly those failures, so
everything here is a genuine repository with genuine commits, created under
pytest's ``tmp_path``.

Layout and relationships:

    shared-contracts    provides the ``contracts`` package (schema + events)
    api-service         depends on the package, provides the orders API
    worker-service      depends on the package, consumes the orders events
    web-client          calls the orders API
    deployment-config   configures all three services

One relationship is deliberately forbidden: ``web-client`` importing
``worker`` source directly. Scenarios rely on it being detected, not absent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from brain.portfolio.models import (
    CrossRepoRelType,
    DependencyContract,
    PortfolioRecord,
    PortfolioRepository,
)
from brain.workspace.models import (
    DEFAULT_CAPABILITIES,
    RepositoryRecord,
    RepositoryType,
    TrustLevel,
    _generate_repository_id,
)
from tests.support import git_fixtures as gf

PORTFOLIO_ID = "portfolio-fixture"

#: Alias → role. The alias is the portfolio-level name; the repository id is
#: derived from the canonical root, so it differs per temporary directory.
ROLES: Dict[str, str] = {
    "shared-contracts": "library",
    "api-service": "service",
    "worker-service": "service",
    "web-client": "client",
    "deployment-config": "configuration",
}

_GITIGNORE = ".brain/\n__pycache__/\n"


def _shared_contracts_files() -> Dict[str, str]:
    return {
        ".gitignore": _GITIGNORE,
        "README.md": "# shared-contracts\n\nOrder schema and event definitions.\n",
        "contracts/__init__.py": 'VERSION = "1.0.0"\n',
        "contracts/schema.py": (
            '"""Order payload schema shared by every service."""\n\n\n'
            "class OrderSchema:\n"
            '    fields = ("order_id", "customer", "total")\n\n'
            "    @staticmethod\n"
            "    def validate(payload):\n"
            "        return all(key in payload for key in OrderSchema.fields)\n"
        ),
        "contracts/events.py": (
            '"""Event definitions published on the orders topic."""\n\n'
            'TOPIC = "orders.created"\n'
            'EVENT_SCHEMA_VERSION = "1"\n\n\n'
            "class OrderCreated:\n"
            '    fields = ("order_id", "created_at")\n'
        ),
    }


def _api_service_files() -> Dict[str, str]:
    return {
        ".gitignore": _GITIGNORE,
        "README.md": "# api-service\n\nExposes the orders API.\n",
        "api/__init__.py": "",
        "api/routes.py": (
            "from contracts.schema import OrderSchema\n\n"
            'ROUTE = "GET /orders"\n\n\n'
            "def list_orders(store):\n"
            "    return [o for o in store if OrderSchema.validate(o)]\n"
        ),
        "api/service.py": (
            "from api.routes import list_orders\n\n\n"
            "class OrderService:\n"
            "    def __init__(self, store):\n"
            "        self.store = store\n\n"
            "    def handle(self):\n"
            "        return list_orders(self.store)\n"
        ),
        "requirements.txt": "shared-contracts==1.0.0\n",
    }


def _worker_service_files() -> Dict[str, str]:
    return {
        ".gitignore": _GITIGNORE,
        "README.md": "# worker-service\n\nConsumes the orders events.\n",
        "worker/__init__.py": "",
        "worker/consumer.py": (
            "from contracts.events import TOPIC, OrderCreated\n"
            "from contracts.schema import OrderSchema\n\n\n"
            "class OrderConsumer:\n"
            "    topic = TOPIC\n\n"
            "    def handle(self, event):\n"
            "        if not OrderSchema.validate(event):\n"
            "            return False\n"
            "        return all(f in event for f in OrderCreated.fields)\n"
        ),
        "requirements.txt": "shared-contracts==1.0.0\n",
    }


def _web_client_files() -> Dict[str, str]:
    return {
        ".gitignore": _GITIGNORE,
        "README.md": "# web-client\n\nCalls the orders API.\n",
        "client/__init__.py": "",
        "client/api_client.py": (
            '"""Thin client over the orders API."""\n\n'
            'ORDERS_ROUTE = "GET /orders"\n\n\n'
            "class OrdersClient:\n"
            "    def __init__(self, transport):\n"
            "        self.transport = transport\n\n"
            "    def fetch(self):\n"
            "        return self.transport.get(ORDERS_ROUTE)\n"
        ),
        # Deliberately forbidden: a client reaching into worker source instead
        # of going through the API. Scenario 8 depends on this being present.
        "client/reporting.py": (
            "from worker.consumer import OrderConsumer\n\n\n"
            "def summarize(events):\n"
            "    consumer = OrderConsumer()\n"
            "    return sum(1 for e in events if consumer.handle(e))\n"
        ),
    }


def _deployment_config_files() -> Dict[str, str]:
    return {
        ".gitignore": _GITIGNORE,
        "README.md": "# deployment-config\n\nService topology.\n",
        "deploy/__init__.py": "",
        "deploy/services.py": (
            '"""Declarative service topology for the portfolio."""\n\n'
            "SERVICES = {\n"
            '    "api-service": {"replicas": 2, "port": 8080},\n'
            '    "worker-service": {"replicas": 1, "queue": "orders.created"},\n'
            '    "web-client": {"replicas": 1, "api": "http://api-service:8080"},\n'
            "}\n"
        ),
    }


_BUILDERS = {
    "shared-contracts": _shared_contracts_files,
    "api-service": _api_service_files,
    "worker-service": _worker_service_files,
    "web-client": _web_client_files,
    "deployment-config": _deployment_config_files,
}


@dataclass
class FixturePortfolio:
    """The materialised fixture: real repositories plus a portfolio record."""

    root: Path
    paths: Dict[str, Path] = field(default_factory=dict)
    records: Dict[str, RepositoryRecord] = field(default_factory=dict)
    portfolio: Optional[PortfolioRecord] = None

    # ── lookup ──

    def path(self, alias: str) -> Path:
        return self.paths[alias]

    def record(self, alias: str) -> RepositoryRecord:
        return self.records[alias]

    def repo_id(self, alias: str) -> str:
        return self.records[alias].repository_id

    def alias_of(self, repository_id: str) -> str:
        for alias, record in self.records.items():
            if record.repository_id == repository_id:
                return alias
        return ""

    def head(self, alias: str) -> str:
        return gf.head(self.paths[alias])

    def revisions(self) -> Dict[str, str]:
        """Current authoritative revision of every repository, by id."""
        return {self.repo_id(a): self.head(a) for a in self.paths}

    def generations(self) -> Dict[str, str]:
        return {self.repo_id(a): f"gen-{a}" for a in self.paths}

    # ── mutation helpers used by scenarios ──

    def commit(self, alias: str, files: Dict[str, str], message: str) -> str:
        return gf.commit_files(self.paths[alias], files, message)

    def refresh_revision(self, alias: str) -> str:
        """Re-read HEAD into the registry record without touching the tree."""
        record = self.records[alias]
        record.current_revision = gf.head(self.paths[alias])
        return record.current_revision


def _contracts() -> List[DependencyContract]:
    """Declared portfolio contracts, keyed by alias."""
    return [
        DependencyContract(
            from_repo="api-service",
            to_repo="shared-contracts",
            dep_type=CrossRepoRelType.DEPENDS_ON_PACKAGE.value,
            allowed=True,
            description="api-service consumes the contracts package",
        ),
        DependencyContract(
            from_repo="worker-service",
            to_repo="shared-contracts",
            dep_type=CrossRepoRelType.DEPENDS_ON_PACKAGE.value,
            allowed=True,
            description="worker-service consumes the contracts package",
        ),
        DependencyContract(
            from_repo="worker-service",
            to_repo="shared-contracts",
            dep_type=CrossRepoRelType.CONSUMES_EVENT.value,
            allowed=True,
            description="worker-service consumes orders.created",
        ),
        DependencyContract(
            from_repo="web-client",
            to_repo="api-service",
            dep_type=CrossRepoRelType.CALLS_API.value,
            allowed=True,
            description="web-client calls GET /orders",
        ),
        DependencyContract(
            from_repo="deployment-config",
            to_repo="api-service",
            dep_type=CrossRepoRelType.DEPLOYS_WITH.value,
            allowed=True,
            description="deployment-config configures api-service",
        ),
        DependencyContract(
            from_repo="deployment-config",
            to_repo="worker-service",
            dep_type=CrossRepoRelType.DEPLOYS_WITH.value,
            allowed=True,
            description="deployment-config configures worker-service",
        ),
        DependencyContract(
            from_repo="deployment-config",
            to_repo="web-client",
            dep_type=CrossRepoRelType.DEPLOYS_WITH.value,
            allowed=True,
            description="deployment-config configures web-client",
        ),
        DependencyContract(
            from_repo="web-client",
            to_repo="worker-service",
            dep_type=CrossRepoRelType.DEPENDS_ON_PACKAGE.value,
            allowed=False,
            description="a client must not import worker source directly",
        ),
    ]


def make_repository_record(
    path: Path,
    alias: str,
    trust_level: TrustLevel = TrustLevel.FIXTURE,
    owner: str = "platform-team",
) -> RepositoryRecord:
    """Build a registry record for one fixture repository."""
    canonical = str(path.resolve())
    normalized = canonical.replace("\\", "/").rstrip("/").lower()
    return RepositoryRecord(
        repository_id=_generate_repository_id(canonical),
        display_name=alias,
        canonical_root=canonical,
        normalized_root_identity=normalized,
        repository_type=RepositoryType.GIT.value,
        default_branch="main",
        current_revision=gf.head(path),
        trust_level=trust_level.value,
        organization="brain-fixtures",
        owner=owner,
        languages=["python"],
        capabilities=[c.value for c in DEFAULT_CAPABILITIES[trust_level]],
        allowed_validation_profiles=["python-compile"],
    )


def build_fixture_portfolio(
    tmp_path: Path,
    dir_name: str = "portfolio-fixture",
    trust_level: TrustLevel = TrustLevel.FIXTURE,
) -> FixturePortfolio:
    """Materialise the five repositories and the portfolio record."""
    root = tmp_path / dir_name
    root.mkdir(parents=True, exist_ok=True)

    fixture = FixturePortfolio(root=root)
    for alias, builder in _BUILDERS.items():
        repo = gf.init_repo(root / alias)
        gf.commit_files(repo, builder(), f"init: {alias}")
        fixture.paths[alias] = repo
        fixture.records[alias] = make_repository_record(repo, alias, trust_level)

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    fixture.portfolio = PortfolioRecord(
        portfolio_id=PORTFOLIO_ID,
        display_name="Orders platform (fixture)",
        repositories=[
            PortfolioRepository(
                repository_id=fixture.repo_id(alias), alias=alias, role=role
            )
            for alias, role in ROLES.items()
        ],
        owners=["platform-team"],
        architecture_policy="clients call APIs; only services import contracts",
        contracts=_contracts(),
        trust_constraints={"minimum_trust_level": trust_level.value},
        created_at_utc=now,
        updated_at_utc=now,
    )
    return fixture


# ── change helpers, one per mandated impact scenario ──


def break_shared_schema(fixture: FixturePortfolio) -> str:
    """Remove a field from the shared order schema — a breaking change."""
    return fixture.commit(
        "shared-contracts",
        {
            "contracts/schema.py": (
                '"""Order payload schema shared by every service."""\n\n\n'
                "class OrderSchema:\n"
                '    fields = ("order_id", "customer")\n\n'
                "    @staticmethod\n"
                "    def validate(payload):\n"
                "        return all(key in payload for key in OrderSchema.fields)\n"
            )
        },
        "break: drop total from the order schema",
    )


def change_event_schema(fixture: FixturePortfolio) -> str:
    return fixture.commit(
        "shared-contracts",
        {
            "contracts/events.py": (
                '"""Event definitions published on the orders topic."""\n\n'
                'TOPIC = "orders.created.v2"\n'
                'EVENT_SCHEMA_VERSION = "2"\n\n\n'
                "class OrderCreated:\n"
                '    fields = ("order_id", "created_at", "channel")\n'
            )
        },
        "change: orders event schema v2",
    )


def change_api_contract(fixture: FixturePortfolio) -> str:
    return fixture.commit(
        "api-service",
        {
            "api/routes.py": (
                "from contracts.schema import OrderSchema\n\n"
                'ROUTE = "GET /v2/orders"\n\n\n'
                "def list_orders(store, page=0):\n"
                "    return [o for o in store if OrderSchema.validate(o)][page:]\n"
            )
        },
        "change: move orders to /v2 and paginate",
    )


def change_deployment(fixture: FixturePortfolio) -> str:
    return fixture.commit(
        "deployment-config",
        {
            "deploy/services.py": (
                '"""Declarative service topology for the portfolio."""\n\n'
                "SERVICES = {\n"
                '    "api-service": {"replicas": 4, "port": 9090},\n'
                '    "worker-service": {"replicas": 3, "queue": "orders.created.v2"},\n'
                '    "web-client": {"replicas": 2, "api": "http://api-service:9090"},\n'
                "}\n"
            )
        },
        "change: scale services and move the API port",
    )


def dependents_of(portfolio: PortfolioRecord, alias: str) -> List[str]:
    """Aliases that declare a dependency on ``alias``, per the contracts.

    Derived from the declared portfolio contracts rather than guessed, so a
    scenario asserting impact is asserting something the portfolio actually
    records.
    """
    return sorted(
        {c.from_repo for c in portfolio.contracts if c.to_repo == alias and c.allowed}
    )
