"""Release-candidate bundle for v0.5.0 — Engineering Change Laboratory.

Produces ``reports/v0.5.0-release-candidate/``: the demonstration artifacts,
the security checks, the performance measurements, the smoke and regression
reports, and the checksum manifest that ties them together.

Everything here is produced by running the shipped code. The demonstrations
use the real five-repository fixture portfolio (real Git repositories, real
commits, real worktrees, real child processes); the smoke and regression
reports are parsed from actual pytest runs; the performance numbers are
measured, not estimated.

The run is staged because the full regression takes minutes and the stages
are independently re-runnable::

    py -3 scripts/v050_release_candidate.py --stage demos
    py -3 scripts/v050_release_candidate.py --stage api-smoke
    py -3 scripts/v050_release_candidate.py --stage cli-smoke
    py -3 scripts/v050_release_candidate.py --stage full-tests
    py -3 scripts/v050_release_candidate.py --stage manifest

Each stage records its own provenance (command, revision, timestamp,
redaction status) into a scratch index that the ``manifest`` stage consumes
and then deletes.

The authoritative checkout is never modified: no patch is applied to it, no
commit is made, no branch is pushed, nothing is deployed.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from v050_engineering_lab_verification import (  # noqa: E402
    Redactor,
    jsonable,
    latency_stats,
    timed,
)

from brain.control.budgets import (  # noqa: E402
    BudgetEnforcer,
    BudgetExceededError,
    BudgetPolicy,
    BudgetUnknownError,
)
from brain.control.plane import ControlPlane  # noqa: E402
from brain.experiments.models import (  # noqa: E402
    ExperimentOption,
    ExportStaleError,
    HumanAction,
)
from brain.experiments.orchestrator import (  # noqa: E402
    ExperimentComparator,
    ExperimentManager,
    ExperimentStore,
)
from brain.freshness.generation_deriver import GenerationDeriver  # noqa: E402
from brain.freshness.incremental_planner import IncrementalPlanner  # noqa: E402
from brain.freshness.models import ArtifactType, FreshnessState  # noqa: E402
from brain.freshness.tracker import FreshnessTracker  # noqa: E402
from brain.graph.builder_v2 import GraphBuilderV2  # noqa: E402
from brain.graph.generation_manager import GraphGenerationManager  # noqa: E402
from brain.lab.engine import (  # noqa: E402
    PatchValidator,
    ValidationRunner,
    _force_remove,
)
from brain.lab.laboratory import ChangeLaboratory  # noqa: E402
from brain.lab.models import (  # noqa: E402
    CommandDef,
    NETWORK_ISOLATION_STATUS,
    PatchRecord,
    ValidationProfile,
    WorkspaceState,
)
from brain.ledger.ledger import EvidenceLedger  # noqa: E402
from brain.ledger.verifier import (  # noqa: E402
    ALTERED_EVENT,
    MISSING_EVENT,
    REORDERED_EVENT,
    LedgerVerifier,
)
from brain.portfolio.graph_builder import (  # noqa: E402
    PortfolioCoupling,
    PortfolioGraphBuilder,
    PortfolioQualityGate,
)
from brain.portfolio.models import (  # noqa: E402
    PortfolioRecord,
    PortfolioRepository,
)
from brain.portfolio.store import PortfolioStore  # noqa: E402
from brain.workspace.capabilities import (  # noqa: E402
    CapabilitiesManager,
    CapabilityDeniedError,
)
from brain.workspace.models import Capability, TrustLevel  # noqa: E402
from brain.workspace.registry_store import RegistryStore  # noqa: E402
from tests.support import git_fixtures as gf  # noqa: E402
from tests.support import portfolio_fixture as pf  # noqa: E402

OUT_DIR = REPO_ROOT / "reports" / "v0.5.0-release-candidate"
# The provenance index lives outside the bundle, in the gitignored metadata
# directory: it must survive a manifest rebuild (stages run one at a time, and
# the manifest stage is the one most likely to be re-run) without ever becoming
# a shipped artifact itself.
PROVENANCE = REPO_ROOT / ".brain" / "v050-rc-provenance.json"
INVENTORY_COMMAND = "py -3 scripts/v050_feature_inventory.py"
MILESTONE = "v0.5.0 Engineering Change Laboratory and Multi-Repository Intelligence"
RC_NAME = "v0.5.0-rc1"
PROFILE = "python-compile"

NON_AUTONOMY_STATEMENT = (
    "Project Brain may apply candidate patches only inside disposable managed "
    "workspaces for validation. It does not apply patches to authoritative "
    "repositories, commit changes, push branches, merge pull requests, or "
    "deploy software."
)


# ──────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def head_revision() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.strip()


def real_patch(repo: Path, rel_path: str, old: str, new: str) -> str:
    """A unified diff with real context, derived from the committed file.

    ``git apply`` refuses zero-context hunks unless told otherwise, and the
    product deliberately does not tell it otherwise — so a demonstration patch
    has to carry the surrounding lines of the actual file.
    """
    target = repo / rel_path
    before = target.read_text(encoding="utf-8").splitlines(keepends=True)
    after = [line.replace(old, new) for line in before]
    body = "".join(
        difflib.unified_diff(before, after, fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}")
    )
    return f"diff --git a/{rel_path} b/{rel_path}\n{body}"


class Bundle:
    """Writes artifacts and records how each one was produced."""

    def __init__(self, out_dir: Path, work_dir: Path, command: str):
        self.out_dir = out_dir
        self.command = command
        self.revision = head_revision()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.redactor = Redactor(
            {
                str(REPO_ROOT): "<repository-root>",
                str(work_dir): "<work-dir>",
                str(Path.home()): "<home>",
                str(Path(tempfile.gettempdir())): "<temp>",
                os.environ.get("USERNAME", "\x00no-user\x00"): "<user>",
            }
        )

    def _record(self, name: str, redacted: bool, command: Optional[str] = None) -> None:
        index = json.loads(PROVENANCE.read_text(encoding="utf-8")) if PROVENANCE.is_file() else {}
        index[name] = {
            "generation_command": command or self.command,
            "git_revision": self.revision,
            "generated_at_utc": utc_now(),
            "redaction_status": "redacted" if redacted else "clean",
        }
        PROVENANCE.parent.mkdir(parents=True, exist_ok=True)
        PROVENANCE.write_text(
            json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )

    def adopt(self, name: str, command: str) -> Path:
        """Record provenance for an artifact a different script wrote.

        The file still has to pass through the redactor — an artifact the
        manifest vouches for cannot carry host paths just because this script
        did not write it.
        """
        path = self.out_dir / name
        raw = path.read_text(encoding="utf-8")
        clean = self.redactor.text(raw)
        if clean != raw:
            path.write_text(clean, encoding="utf-8", newline="\n")
        self._record(name, redacted=clean != raw, command=command)
        print(f"  adopted {name}")
        return path

    def write(self, name: str, payload: Any) -> Path:
        raw = json.dumps(jsonable(payload), indent=2, sort_keys=True, ensure_ascii=False)
        clean = self.redactor.text(raw)
        path = self.out_dir / name
        path.write_text(clean + "\n", encoding="utf-8", newline="\n")
        self._record(name, redacted=clean != raw)
        print(f"  wrote {name}")
        return path

    def write_text(self, name: str, text: str) -> Path:
        clean = self.redactor.text(text)
        path = self.out_dir / name
        path.write_text(clean, encoding="utf-8", newline="\n")
        self._record(name, redacted=clean != text)
        print(f"  wrote {name}")
        return path


# ──────────────────────────────────────────────
# Stage: demonstrations, security checks, performance
# ──────────────────────────────────────────────


class Demos:
    """One continuous portfolio lifecycle, recorded stage by stage."""

    def __init__(self, bundle: Bundle, work: Path):
        self.b = bundle
        self.work = work
        self.brain = work / ".brain"
        self.brain.mkdir(parents=True, exist_ok=True)
        self.fixture = pf.build_fixture_portfolio(
            work, trust_level=TrustLevel.TRUSTED_INTERNAL
        )
        self.perf: Dict[str, Dict[str, Any]] = {
            "small_fixture_portfolio": {},
            "medium_generated_portfolio": {},
            "real_project_brain_repository": {},
        }

    def run(self) -> None:
        print("Phase 1: registry")
        self.registry_demo()
        print("Phase 2: portfolio")
        self.portfolio_demo()
        print("Phase 3: incremental")
        self.incremental_demo()
        print("Phase 4: freshness")
        self.freshness_demo()
        print("Phase 5: workspace")
        self.workspace_demo()
        print("Phase 6: experiment")
        self.experiment_demo()
        print("Phase 7: patch export")
        self.export_demo()
        print("Phase 8: ledger")
        self.ledger_demo()
        print("Phase 9: security checks")
        self.security_checks()
        # The control plane is read last on purpose: its summary is the bundle's
        # claim about leaked workspaces, so it must observe every workspace the
        # earlier phases created, including the ones the security probes made.
        print("Phase 10: control plane")
        self.control_plane_demo()
        print("Phase 11: performance")
        self.performance()

    # ── phase 1 ──

    def registry_demo(self) -> None:
        self.registry = RegistryStore(self.brain / "workspace")
        self.ledger = EvidenceLedger(self.brain)
        register_ms: List[float] = []
        for alias in pf.ROLES:
            _rec, ms = timed(
                lambda alias=alias: self.registry.register(
                    self.fixture.record(alias),
                    actor="release-candidate",
                    source="script",
                    reason="v0.5.0 release-candidate demonstration",
                )
            )
            register_ms.append(ms)
            self.ledger.record_repository_registered(
                self.fixture.repo_id(alias),
                actor_identity="release-candidate",
                trust_level=TrustLevel.TRUSTED_INTERNAL.value,
                metadata={"alias": alias},
            )

        duplicate: str
        try:
            self.registry.register(self.fixture.record("api-service"))
            duplicate = "accepted — DEFECT"
        except ValueError as exc:
            duplicate = f"rejected: {exc}"

        lookups = []
        target = self.fixture.repo_id("api-service")
        for _ in range(200):
            _value, ms = timed(lambda: self.registry.get(target))
            lookups.append(ms)
        integrity_ok, integrity_message = self.registry.verify_integrity()

        self.perf["small_fixture_portfolio"]["registry_register_ms"] = round(
            statistics.median(register_ms), 3
        )
        self.perf["small_fixture_portfolio"]["registry_lookup"] = latency_stats(lookups)

        registered: List[Dict[str, Any]] = []
        for alias in pf.ROLES:
            registered_record = self.registry.get(self.fixture.repo_id(alias))
            assert registered_record is not None
            registered.append(
                registered_record.portable_export() | {"normalized_root_identity": "<redacted>"}
            )

        self.b.write(
            "registry-demo.json",
            {
                "registered": registered,
                "repository_count": len(self.registry.list_all()),
                "trust_level": TrustLevel.TRUSTED_INTERNAL.value,
                "duplicate_registration": duplicate,
                "integrity": {"valid": integrity_ok, "message": integrity_message},
                "events": [jsonable(e) for e in self.registry.list_events()],
            },
        )

    # ── phase 2 ──

    def portfolio_demo(self) -> None:
        portfolio = self.fixture.portfolio
        assert portfolio is not None
        self.portfolio_store = PortfolioStore(self.brain / "portfolio")
        self.portfolio_store.save_portfolio(portfolio)

        self.repo_generations: Dict[str, str] = {}
        graph_ms: List[float] = []
        for alias in ("shared-contracts", "api-service", "worker-service", "web-client"):
            repo = self.fixture.path(alias)
            (meta, _report), ms = timed(
                lambda repo=repo: GraphBuilderV2(repo).build_generation(gf.head(repo))
            )
            GraphGenerationManager(repo / ".brain").activate_generation(meta.generation_id)
            self.repo_generations[self.fixture.repo_id(alias)] = meta.generation_id
            graph_ms.append(ms)

        builder = PortfolioGraphBuilder(portfolio)
        generation, build_ms = timed(
            lambda: builder.build_generation(
                self.fixture.revisions(), self.fixture.generations()
            )
        )
        gate = PortfolioQualityGate.validate(generation, portfolio)
        coupling = PortfolioCoupling.calculate(generation, portfolio)
        builder.activate(generation)
        self.portfolio_store.save_generation(generation)
        self.generation = generation

        self.perf["small_fixture_portfolio"]["repository_graph_full_build_ms"] = round(
            statistics.median(graph_ms), 3
        )
        self.perf["small_fixture_portfolio"]["portfolio_build_ms"] = build_ms

        self.b.write(
            "portfolio-demo.json",
            {
                "portfolio": jsonable(self.fixture.portfolio),
                "generation": jsonable(generation),
                "status": generation.status,
                "quality_gate": jsonable(gate),
                "coupling": jsonable(coupling),
                "declared_contracts": len(portfolio.contracts),
                "repository_graphs_built": self.repo_generations,
                "portfolio_build_duration_ms": build_ms,
            },
        )

    # ── phase 3 ──

    def incremental_demo(self) -> None:
        base_api = self.fixture.head("api-service")
        self.broken_contract = pf.break_shared_schema(self.fixture)
        candidate_api = pf.change_api_contract(self.fixture)

        planner = IncrementalPlanner()
        plan, plan_ms = timed(
            lambda: planner.plan_update(
                self.fixture.path("api-service"),
                base_api,
                candidate_api,
                repository_id="api-service",
            )
        )
        fallback, fallback_reason = IncrementalPlanner.should_fallback(plan)
        deriver = GenerationDeriver(self.fixture.path("api-service"), "api-service")
        report, derive_ms = timed(
            lambda: deriver.derive(
                plan, self.repo_generations[self.fixture.repo_id("api-service")], activate=True
            )
        )
        self.incremental = report
        self.candidate_api = candidate_api

        # The same planner over an unrelated pair of revisions, to show the
        # full-rebuild branch is reachable and not merely declared.
        wide = planner.plan_update(
            self.fixture.path("shared-contracts"),
            gf.head(self.fixture.path("shared-contracts")),
            gf.head(self.fixture.path("shared-contracts")),
            repository_id="shared-contracts",
        )

        self.perf["small_fixture_portfolio"]["incremental_plan_ms"] = plan_ms
        self.perf["small_fixture_portfolio"]["incremental_derive_ms"] = derive_ms

        self.b.write(
            "incremental-demo.json",
            {
                "repository": "api-service",
                "base_revision": base_api,
                "candidate_revision": candidate_api,
                "plan": jsonable(plan),
                "decision": plan.decision,
                "fallback_to_full_rebuild": {
                    "required": fallback,
                    "reason": fallback_reason,
                },
                "derivation": jsonable(report),
                "no_change_plan": {
                    "decision": wide.decision,
                    "modified_files": wide.modified_files,
                },
                "plan_duration_ms": plan_ms,
                "derivation_duration_ms": derive_ms,
            },
        )

    # ── phase 4 ──

    def freshness_demo(self) -> None:
        self.tracker = FreshnessTracker(self.brain / "freshness")
        record_ms: List[float] = []
        for alias in ("shared-contracts", "api-service", "worker-service", "web-client"):
            repo = self.fixture.path(alias)
            _rec, ms = timed(
                lambda alias=alias, repo=repo: self.tracker.record_from_repository(
                    artifact_id=f"graph:{self.fixture.repo_id(alias)}",
                    artifact_type=ArtifactType.REPOSITORY_GRAPH,
                    repository_id=self.fixture.repo_id(alias),
                    repo_path=repo,
                    source_revision=gf.head(repo),
                    evidence=json.dumps(
                        {"generation_id": self.repo_generations[self.fixture.repo_id(alias)]}
                    ),
                )
            )
            record_ms.append(ms)

        portfolio = self.fixture.portfolio
        assert portfolio is not None
        impacted = pf.dependents_of(portfolio, "shared-contracts")
        for alias in impacted:
            self.tracker.invalidate(
                f"graph:{self.fixture.repo_id(alias)}",
                caused_by="shared-contracts",
                details=f"breaking schema change {self.broken_contract[:12]}",
            )
        records = self.tracker.list_all()
        stale = [r.artifact_id for r in records if r.state == FreshnessState.STALE.value]

        self.perf["small_fixture_portfolio"]["freshness_record_ms"] = round(
            statistics.median(record_ms), 3
        )

        self.b.write(
            "freshness-demo.json",
            {
                "tracked_artifacts": len(records),
                "impacted_by_shared_contract_change": impacted,
                "stale_artifacts": stale,
                "records": [jsonable(r) for r in records],
                "explain_stale": jsonable(
                    self.tracker.explain(f"graph:{self.fixture.repo_id('api-service')}")
                ),
                "explain_fresh": jsonable(
                    self.tracker.explain(f"graph:{self.fixture.repo_id('web-client')}")
                ),
            },
        )

    # ── phase 5 ──

    def workspace_demo(self) -> None:
        self.lab = ChangeLaboratory(self.brain)
        self.client_repo = self.fixture.path("web-client")
        self.client_id = self.fixture.repo_id("web-client")
        self.client_rev = self.fixture.head("web-client")

        self.patch_a = PatchRecord(
            patch_id="rc-patch-a",
            repository_id=self.client_id,
            base_revision=self.client_rev,
            patch_content=real_patch(
                self.client_repo,
                "client/reporting.py",
                "from worker.consumer import OrderConsumer",
                "from worker.consumer import OrderConsumer  # noqa: retained",
            ),
            allowed_paths=["client"],
        )
        self.patch_b = PatchRecord(
            patch_id="rc-patch-b",
            repository_id=self.client_id,
            base_revision=self.client_rev,
            patch_content=real_patch(
                self.client_repo,
                "client/reporting.py",
                "from worker.consumer import OrderConsumer",
                "from client.api_client import OrdersClient",
            ),
            allowed_paths=["client"],
        )

        session_a, session_a_ms = timed(
            lambda: self.lab.run_session(
                repository_id=self.client_id,
                repo_path=self.client_repo,
                base_revision=self.client_rev,
                patch=self.patch_a,
                profile_name=PROFILE,
                cleanup=False,
            )
        )
        session_b = self.lab.run_session(
            repository_id=self.client_id,
            repo_path=self.client_repo,
            base_revision=self.client_rev,
            patch=self.patch_b,
            profile_name=PROFILE,
            cleanup=False,
        )
        self.sessions = [session_a, session_b]

        manager = self.lab.manager
        workspace_paths = {}
        for s in self.sessions:
            session_workspace = manager.get_workspace(s["workspace_id"])
            assert session_workspace is not None
            workspace_paths[s["workspace_id"]] = manager.workspace_path(session_workspace)
        workspace_bytes = sum(dir_bytes(p) for p in workspace_paths.values())

        cleanups = []
        cleanup_ms: List[float] = []
        for session in self.sessions:
            report, ms = timed(
                lambda session=session: manager.clean_workspace(
                    session["workspace_id"], reason="release-candidate demonstration complete"
                )
            )
            cleanups.append(report)
            cleanup_ms.append(ms)

        self.perf["small_fixture_portfolio"]["workspace_session_ms"] = session_a_ms
        self.perf["small_fixture_portfolio"]["patch_apply_ms"] = round(
            session_a["apply"].get("duration_ms", 0), 3
        )
        self.perf["small_fixture_portfolio"]["validation_duration_seconds"] = session_a[
            "validation"
        ]["duration_seconds"]
        self.perf["small_fixture_portfolio"]["workspace_cleanup_ms"] = round(
            statistics.median(cleanup_ms), 3
        )
        self.perf["small_fixture_portfolio"]["workspace_bytes"] = workspace_bytes

        self.b.write(
            "workspace-demo.json",
            {
                "sessions": [jsonable(s) for s in self.sessions],
                "state_transitions": [
                    "created",
                    "patching",
                    "validating",
                    "passed",
                    "cleaned",
                ],
                "commands_executed": [
                    " ".join(r.get("argv", []))
                    for s in self.sessions
                    for r in s["validation"]["results"]
                ],
                "cleanup": [jsonable(c) for c in cleanups],
                "directories_removed": [not p.exists() for p in workspace_paths.values()],
                "workspace_bytes": workspace_bytes,
                "profiles_available": self.lab.list_profiles(),
                "isolation": {
                    "filesystem": "Git worktree under the managed workspace root",
                    "process": "new process group, terminated on timeout or cancellation",
                    "environment": "allowlisted variables only; secret-like names refused",
                    "network": NETWORK_ISOLATION_STATUS,
                },
            },
        )

    # ── phase 6 ──

    def experiment_demo(self) -> None:
        session_a, session_b = self.sessions
        option_a = ExperimentOption(
            option_id="opt-a",
            source_remediation_option="suppress-the-warning",
            candidate_patch=self.patch_a.patch_content,
            patch_provenance="generated by the v0.5.0 release-candidate script",
            expected_files=["client/reporting.py"],
            expected_effect="Keeps the cross-repository import and annotates it",
            validation_profile=PROFILE,
            executed=True,
            passed_required=True,
            architecture_improved=False,
            forbidden_edge_delta=0,
            resolved_findings=0,
            changed_files=1,
            changed_lines=2,
            validation_duration=session_a["validation"]["duration_seconds"],
        )
        option_b = ExperimentOption(
            option_id="opt-b",
            source_remediation_option="call-the-api-instead",
            candidate_patch=self.patch_b.patch_content,
            patch_provenance="generated by the v0.5.0 release-candidate script",
            expected_files=["client/reporting.py"],
            expected_effect="Replaces the forbidden import with the API client",
            validation_profile=PROFILE,
            executed=True,
            passed_required=True,
            architecture_improved=True,
            forbidden_edge_delta=-1,
            resolved_findings=1,
            changed_files=1,
            changed_lines=2,
            validation_duration=session_b["validation"]["duration_seconds"],
        )

        self.store = ExperimentStore(self.brain / "experiments")
        self.manager = ExperimentManager(self.store)
        self.experiment = self.manager.create_experiment(
            repository_id=self.client_id,
            base_revision=self.client_rev,
            options=[option_a, option_b],
            creation_actor="release-candidate-operator",
            validation_profile=PROFILE,
        )
        comparison, compare_ms = timed(
            lambda: self.manager.complete_comparison(self.experiment.experiment_id)
        )
        final = self.store.get(self.experiment.experiment_id)
        assert final is not None

        self.perf["small_fixture_portfolio"]["experiment_comparison_ms"] = compare_ms

        self.b.write(
            "experiment-demo.json",
            {
                "experiment": jsonable(final),
                "comparison": jsonable(comparison),
                "conclusion": final.conclusion,
                "recommended_option_id": final.recommended_option_id,
                "comparison_formula": ExperimentComparator.formula(),
                "comparison_duration_ms": compare_ms,
                "compared_by": "deterministic weighted scoring — no language model",
                "authoritative_repository_modified": False,
            },
        )
        self.final_experiment = final
        self.comparison = comparison

    # ── phase 7 ──

    def export_demo(self) -> None:
        experiment_id = self.experiment.experiment_id
        assert self.final_experiment is not None
        option_id = self.final_experiment.recommended_option_id

        before_accept: str
        try:
            self.manager.export_patch(
                experiment_id, option_id, authoritative_revision=self.client_rev
            )
            before_accept = "exported — DEFECT: no human acceptance was recorded"
        except Exception as exc:  # the product raises a typed refusal
            before_accept = f"{type(exc).__name__}: {exc}"

        self.manager.record_human_action(
            experiment_id,
            HumanAction.ACCEPT_FOR_EXPORT,
            actor="release-candidate-operator",
            option_id=option_id,
            note="accepted by the operator running the release-candidate script",
        )
        export, export_ms = timed(
            lambda: self.manager.export_patch(
                experiment_id, option_id, authoritative_revision=self.client_rev
            )
        )
        self.export = export

        # The authoritative fixture repository still holds the original import:
        # exporting is producing a file, not applying it.
        source_after = (self.client_repo / "client" / "reporting.py").read_text(
            encoding="utf-8"
        )

        # A later authoritative commit must invalidate the export.
        pf.change_deployment(self.fixture)
        moved_revision = gf.head(self.client_repo)
        stale_result: str
        try:
            self.manager.export_patch(
                experiment_id, option_id, authoritative_revision="0" * 40
            )
            stale_result = "exported — DEFECT: stale base revision accepted"
        except ExportStaleError as exc:
            stale_result = f"ExportStaleError: {exc}"

        self.perf["small_fixture_portfolio"]["patch_export_ms"] = export_ms

        self.b.write(
            "patch-export-demo.json",
            {
                "export": jsonable(export),
                "export_before_human_acceptance": before_accept,
                "human_action": HumanAction.ACCEPT_FOR_EXPORT.value,
                "authorized_by": export.authorized_by,
                "applied_to_authoritative_repository": False,
                "authoritative_source_still_contains_original_import": (
                    "from worker.consumer import OrderConsumer" in source_after
                ),
                "authoritative_revision_unchanged": moved_revision == self.client_rev,
                "stale_export_attempt": stale_result,
                "export_duration_ms": export_ms,
            },
        )

    # ── phase 8 ──

    def ledger_demo(self) -> None:
        _event, append_ms = timed(
            lambda: self.ledger.record_patch_exported(
                self.client_id,
                self.export.export_id,
                experiment_id=self.export.experiment_id,
                option_id=self.export.option_id,
                patch_hash=hashlib.sha256(
                    self.export.unified_diff.encode("utf-8")
                ).hexdigest(),
                actor_identity="release-candidate-operator",
                source_revision=self.client_rev,
            )
        )
        self.ledger.record_graph_generation_built(
            self.fixture.repo_id("api-service"),
            self.incremental.new_generation_id,
            actor_identity="release-candidate",
            metadata={"decision": self.incremental.decision},
        )
        events = self.ledger.store.all_events()
        report, verify_ms = timed(lambda: LedgerVerifier.verify(events))
        self.ledger_report = report

        self.perf["small_fixture_portfolio"]["ledger_append_ms"] = append_ms
        self.perf["small_fixture_portfolio"]["ledger_verify_ms"] = verify_ms

        exported = next(e for e in events if e.event_type == "patch_exported")

        self.b.write(
            "ledger-demo.json",
            {
                "events_checked": report.get("events_checked"),
                "valid": report.get("valid"),
                "issues": report.get("issues"),
                "head_sequence": report.get("head_sequence"),
                "event_types": sorted({e.event_type for e in events}),
                "patch_exported_event": {
                    "event_type": exported.event_type,
                    "applied_to_repository": exported.metadata.get(
                        "applied_to_repository"
                    ),
                },
                "append_only": (
                    "SQLite BEFORE UPDATE and BEFORE DELETE triggers reject any "
                    "mutation; there is no generic writer, only named recorders."
                ),
                "append_duration_ms": append_ms,
                "verify_duration_ms": verify_ms,
            },
        )

    # ── phase 9 ──

    def control_plane_demo(self) -> None:
        plane = ControlPlane(self.brain)
        summary, summary_ms = timed(plane.summary)
        queue, queue_ms = timed(lambda: plane.action_queue())
        self.control_summary = summary

        self.perf["small_fixture_portfolio"]["control_summary_ms"] = summary_ms
        self.perf["small_fixture_portfolio"]["control_action_queue_ms"] = queue_ms
        self.perf["small_fixture_portfolio"]["artifact_storage_bytes"] = dir_bytes(self.brain)

        self.b.write(
            "control-plane-demo.json",
            {
                "summary": jsonable(summary),
                "action_queue": jsonable(queue),
                "budgets": jsonable(plane.budgets()),
                "observed_usage": jsonable(plane.observed_usage()),
                "metrics": jsonable(plane.metrics()),
                "health": jsonable(plane.health()),
                "active_workspaces": summary["workspaces"]["active"],
                "quarantined_workspaces": summary["workspaces"]["quarantined"],
                "summary_duration_ms": summary_ms,
                "action_queue_duration_ms": queue_ms,
            },
        )

    # ── phase 10 ──

    def security_checks(self) -> None:
        checks: List[Dict[str, Any]] = []

        def check(name: str, expectation: str, outcome: str, passed: bool) -> None:
            checks.append(
                {
                    "check": name,
                    "expectation": expectation,
                    "observed": outcome,
                    "passed": passed,
                }
            )

        # Trust levels and capabilities
        for level in (TrustLevel.UNTRUSTED_EXTERNAL, TrustLevel.TRUSTED_READ_ONLY):
            defaults = CapabilitiesManager.get_defaults(level)
            try:
                CapabilitiesManager.require("probe", defaults, Capability.APPLY_CANDIDATE_PATCHES)
                check(
                    f"capability denial for {level.value}",
                    "apply_candidate_patches is denied",
                    "allowed",
                    False,
                )
            except CapabilityDeniedError as exc:
                check(
                    f"capability denial for {level.value}",
                    "apply_candidate_patches is denied",
                    f"denied: {exc}",
                    True,
                )

        # Patch admission
        traversal = PatchRecord(
            patch_id="sec-traversal",
            repository_id=self.client_id,
            patch_content=(
                "diff --git a/../../etc/passwd b/../../etc/passwd\n"
                "--- a/../../etc/passwd\n+++ b/../../etc/passwd\n"
                "@@ -1 +1 @@\n-root\n+attacker\n"
            ),
        )
        reasons = PatchValidator.validate(traversal)
        check(
            "path traversal in candidate patch",
            "rejected before any workspace exists",
            "; ".join(reasons) or "accepted",
            any("Path traversal" in r for r in reasons),
        )

        git_dir = PatchRecord(
            patch_id="sec-gitdir",
            repository_id=self.client_id,
            patch_content=(
                "diff --git a/.git/config b/.git/config\n"
                "--- a/.git/config\n+++ b/.git/config\n"
                "@@ -1 +1 @@\n-[core]\n+[remote]\n"
            ),
        )
        reasons = PatchValidator.validate(git_dir)
        check(
            "candidate patch writing into .git",
            "rejected",
            "; ".join(reasons) or "accepted",
            any(".git" in r for r in reasons),
        )

        out_of_scope = PatchRecord(
            patch_id="sec-scope",
            repository_id=self.client_id,
            patch_content=real_patch(
                self.client_repo,
                "client/reporting.py",
                "from client.api_client import OrdersClient",
                "from client.api_client import OrdersClient  # scope probe",
            ),
            allowed_paths=["deploy"],
        )
        reasons = PatchValidator.validate(out_of_scope)
        check(
            "candidate patch outside declared scope",
            "rejected",
            "; ".join(reasons) or "accepted",
            any("outside declared scope" in r for r in reasons),
        )

        stale = PatchRecord(
            patch_id="sec-stale",
            repository_id=self.client_id,
            base_revision=self.client_rev,
            patch_content=self.patch_b.patch_content,
            expected_file_hashes={"client/reporting.py": "0" * 64},
            allowed_paths=["client"],
        )
        session = self.lab.run_session(
            repository_id=self.client_id,
            repo_path=self.client_repo,
            base_revision=self.client_rev,
            patch=stale,
            profile_name=PROFILE,
        )
        check(
            "stale candidate patch",
            "rejected: the base file no longer hashes to the expected value",
            f"{session['outcome']}: {'; '.join(session['rejected_reasons'])}",
            session["outcome"] == "rejected"
            and any("stale" in r for r in session["rejected_reasons"]),
        )

        # Validation profiles
        hostile = ValidationProfile(
            profile_name="hostile",
            commands=[
                CommandDef(argv=["curl", "https://example.invalid"]),
                CommandDef(argv=["/usr/bin/python", "-c", "print(1)"]),
            ],
            environment={"API_KEY": "leak-me"},
        )
        issues = ValidationRunner.validate_profile(hostile)
        check(
            "validation profile allowlist",
            "unlisted command, absolute executable and secret-like variable all rejected",
            "; ".join(issues) or "accepted",
            any("not in allowlist" in i for i in issues)
            and any("executable path not allowed" in i for i in issues)
            and any("secret-like variable" in i for i in issues),
        )

        denied = self.lab.run_session(
            repository_id=self.client_id,
            repo_path=self.client_repo,
            base_revision=self.client_rev,
            patch=self.patch_b,
            profile_name=PROFILE,
            allowed_profiles=["some-other-profile"],
        )
        check(
            "validation profile not allowed for the repository",
            "rejected before a workspace is created",
            f"{denied['outcome']}: {'; '.join(denied['rejected_reasons'])}",
            denied["outcome"] == "rejected" and "workspace_id" not in denied,
        )

        # Static: no shell strings anywhere in the shipped packages. Parsed
        # rather than grepped — the laboratory's own docstring says the words
        # "shell=True" while promising never to use it.
        shell_hits = []
        for source in list((REPO_ROOT / "brain").rglob("*.py")) + list(
            (REPO_ROOT / "apps").rglob("*.py")
        ):
            tree = ast.parse(source.read_text(encoding="utf-8", errors="replace"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and any(
                    kw.arg == "shell"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                    for kw in node.keywords
                ):
                    shell_hits.append(f"{source.relative_to(REPO_ROOT)}:{node.lineno}")
        check(
            "no shell=True in shipped code",
            "brain/ and apps/ never spawn a shell",
            f"{len(shell_hits)} occurrences: {shell_hits}" if shell_hits else "none",
            not shell_hits,
        )

        # Workspace ownership
        manager = self.lab.manager
        record = manager.create_workspace(
            repository_id=self.fixture.repo_id("api-service"),
            base_revision=self.fixture.head("api-service"),
            repo_path=self.fixture.path("api-service"),
        )
        managed_workspace = manager.get_workspace(record.workspace_id)
        assert managed_workspace is not None
        managed_root = managed_workspace.workspace_root
        manager.update_state(
            record.workspace_id,
            WorkspaceState.FAILED,
            workspace_root=str(self.fixture.path("api-service")),
        )
        report = manager.clean_workspace(record.workspace_id, reason="ownership probe")
        survivor = (self.fixture.path("api-service") / "api" / "routes.py").is_file()
        check(
            "cleanup of a path outside the managed root",
            "refused and quarantined; nothing outside the root deleted",
            f"quarantined={report.quarantined}, removed={report.removed}, "
            f"source_intact={survivor}",
            report.quarantined and not report.removed and survivor,
        )
        # Restore provable ownership and clean for real, so the probe leaves
        # neither a quarantined record nor an orphaned directory behind.
        manager.update_state(
            record.workspace_id, WorkspaceState.FAILED, workspace_root=managed_root
        )
        recovered = manager.clean_workspace(
            record.workspace_id, reason="ownership probe complete"
        )
        check(
            "recovery of the quarantined workspace",
            "removed once ownership can be proven again",
            f"removed={recovered.removed}, "
            f"directory_present={Path(managed_root).exists()}",
            recovered.removed and not Path(managed_root).exists(),
        )

        # Ledger tamper detection
        events = self.ledger.store.all_events()
        tampered = list(events)
        tampered[1].reason = "silently rewritten"
        altered = LedgerVerifier.verify(tampered)
        check(
            "modified ledger event",
            f"detected as {ALTERED_EVENT}",
            json.dumps(altered.get("issues", [])),
            not altered["valid"]
            and any(i["code"] == ALTERED_EVENT for i in altered["issues"]),
        )

        fresh = self.ledger.store.all_events()
        missing = LedgerVerifier.verify(fresh[:1] + fresh[2:])
        check(
            "missing ledger event",
            f"detected as {MISSING_EVENT}",
            json.dumps(missing.get("issues", [])),
            not missing["valid"]
            and any(i["code"] == MISSING_EVENT for i in missing["issues"]),
        )

        fresh = self.ledger.store.all_events()
        reordered = LedgerVerifier.verify([fresh[1], fresh[0]] + fresh[2:])
        check(
            "reordered ledger event",
            f"detected as {REORDERED_EVENT}",
            json.dumps(reordered.get("issues", [])),
            not reordered["valid"]
            and any(i["code"] == REORDERED_EVENT for i in reordered["issues"]),
        )

        # Budgets fail closed
        enforcer = BudgetEnforcer(BudgetPolicy(max_active_workspaces=1))
        try:
            enforcer.check("max_active_workspaces", None)
            check("budget with unobservable usage", "fails closed", "allowed", False)
        except BudgetUnknownError as exc:
            check("budget with unobservable usage", "fails closed", f"{exc}", True)
        try:
            enforcer.check("max_active_workspaces", 1)
            check("budget over the limit", "refused", "allowed", False)
        except BudgetExceededError as exc:
            check("budget over the limit", "refused", f"{exc}", True)

        self.b.write(
            "security-checks.json",
            {
                "checks": checks,
                "passed": sum(1 for c in checks if c["passed"]),
                "failed": sum(1 for c in checks if not c["passed"]),
                "network_isolation": {
                    "status": NETWORK_ISOLATION_STATUS,
                    "note": (
                        "No OS-level network isolation is available on this host, so "
                        "the claim is not made. Validation child processes can reach "
                        "the network with the privileges of the invoking user."
                    ),
                },
                "non_autonomy_statement": NON_AUTONOMY_STATEMENT,
            },
        )
        self.security_summary = {
            "passed": sum(1 for c in checks if c["passed"]),
            "failed": sum(1 for c in checks if not c["passed"]),
        }

    # ── phase 11 ──

    def performance(self) -> None:
        self.medium_portfolio()
        self.real_repository()

        engineering_lab = REPO_ROOT / "reports" / "v0.5.0-engineering-lab" / "performance.json"
        borrowed: Dict[str, Any] = {}
        if engineering_lab.is_file():
            measured = json.loads(engineering_lab.read_text(encoding="utf-8"))["measurements"]
            borrowed = {
                key: measured[key]
                for key in (
                    "workspace_create_ms",
                    "workspace_cleanup_ms",
                    "workspace_bytes",
                    "patch_apply_ms",
                    "post_patch_analysis_ms",
                    "experiment_run_ms",
                    "validation_duration_seconds",
                )
                if key in measured
            }
        self.perf["real_project_brain_repository"]["from_engineering_lab_run"] = {
            "source": "reports/v0.5.0-engineering-lab/performance.json",
            "reason": (
                "These come from the real-repository verification run rather than "
                "being re-measured here: they require a disposable worktree of the "
                "authoritative checkout, and one such run is the evidence."
            ),
            "measurements": borrowed,
        }

        self.b.write(
            "performance.json",
            {
                "environment": {
                    "python": sys.version.split()[0],
                    "platform": sys.platform,
                    "cpu_count": os.cpu_count(),
                },
                "method": (
                    "Wall-clock timings from a single host and a single run, except "
                    "registry lookup, which is 200 repeated measurements reported as "
                    "p50/p95. Percentiles are given only where repetition was "
                    "practical. These are the observed numbers; they support no "
                    "capacity claim beyond the scales listed."
                ),
                "scales": self.perf,
            },
        )

    def medium_portfolio(self) -> None:
        """Ten generated repositories, each with thirty modules."""
        root = self.work / "medium"
        root.mkdir(parents=True, exist_ok=True)
        registry = RegistryStore(self.brain / "medium-registry")
        repositories: List[PortfolioRepository] = []
        revisions: Dict[str, str] = {}
        generations: Dict[str, str] = {}
        paths: List[Path] = []

        for index in range(10):
            name = f"generated-{index:02d}"
            modules = {
                f"pkg/module_{n:02d}.py": (
                    f"from pkg.core import Core\n\n\n"
                    f"class Module{n:02d}:\n"
                    f"    def run(self):\n"
                    f"        return Core().run()\n"
                )
                for n in range(30)
            }
            repo = gf.make_python_repo(root, name=name, extra_files=modules)
            paths.append(repo)
            record = pf.make_repository_record(repo, name, TrustLevel.TRUSTED_INTERNAL)
            registry.register(record)
            repositories.append(
                PortfolioRepository(
                    repository_id=record.repository_id, alias=name, role="service"
                )
            )
            revisions[record.repository_id] = gf.head(repo)
            generations[record.repository_id] = ""

        lookups = []
        target = repositories[0].repository_id
        for _ in range(200):
            _value, ms = timed(lambda: registry.get(target))
            lookups.append(ms)

        (meta, _report), graph_ms = timed(
            lambda: GraphBuilderV2(paths[0]).build_generation(gf.head(paths[0]))
        )
        generations[repositories[0].repository_id] = meta.generation_id

        now = utc_now()
        portfolio = PortfolioRecord(
            portfolio_id="medium-generated",
            display_name="Generated medium portfolio",
            repositories=repositories,
            owners=["release-candidate"],
            architecture_policy="generated fixture; no declared contracts",
            contracts=[],
            trust_constraints={
                "minimum_trust_level": TrustLevel.TRUSTED_INTERNAL.value
            },
            created_at_utc=now,
            updated_at_utc=now,
        )
        builder = PortfolioGraphBuilder(portfolio)
        generation, build_ms = timed(
            lambda: builder.build_generation(revisions, generations)
        )

        self.perf["medium_generated_portfolio"] = {
            "repositories": len(repositories),
            "modules_per_repository": 32,
            "registry_lookup": latency_stats(lookups),
            "repository_graph_full_build_ms": graph_ms,
            "portfolio_build_ms": build_ms,
            "portfolio_generation_status": generation.status,
            "artifact_storage_bytes": dir_bytes(self.brain / "medium-registry"),
        }

    def real_repository(self) -> None:
        """Read-only measurements against the authoritative checkout."""
        registry = RegistryStore(self.brain / "real-registry")
        record = pf.make_repository_record(
            REPO_ROOT, "project-brain", TrustLevel.TRUSTED_INTERNAL
        )
        _saved, register_ms = timed(lambda: registry.register(record))
        lookups = []
        for _ in range(200):
            _value, ms = timed(lambda: registry.get(record.repository_id))
            lookups.append(ms)

        builder = GraphBuilderV2(REPO_ROOT)
        (meta, report), graph_ms = timed(
            lambda: builder.build_generation(record.current_revision)
        )
        metrics = jsonable(meta).get("metrics") or jsonable(report).get("metrics") or {}

        base = subprocess.run(
            ["git", "rev-parse", "HEAD~1"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout.strip()
        planner = IncrementalPlanner()
        plan, plan_ms = timed(
            lambda: planner.plan_update(
                REPO_ROOT, base, record.current_revision, repository_id=record.repository_id
            )
        )
        deriver = GenerationDeriver(REPO_ROOT, builder.repo_id)
        derived, derive_ms = timed(
            lambda: deriver.derive(plan, meta.generation_id, activate=False)
        )

        ledger = EvidenceLedger(self.brain / "real-ledger")
        _event, append_ms = timed(
            lambda: ledger.record_repository_registered(
                record.repository_id,
                actor_identity="release-candidate",
                trust_level=record.trust_level,
                source_revision=record.current_revision,
            )
        )
        events = ledger.store.all_events()
        _verified, verify_ms = timed(lambda: LedgerVerifier.verify(events))

        self.perf["real_project_brain_repository"].update(
            {
                "revision": record.current_revision,
                "registry_register_ms": register_ms,
                "registry_lookup": latency_stats(lookups),
                "repository_graph_full_build_ms": graph_ms,
                "graph_nodes": sum((metrics.get("node_counts_by_type") or {}).values()),
                "graph_relationships": sum((metrics.get("rel_counts_by_type") or {}).values()),
                "incremental_plan_ms": plan_ms,
                "incremental_derive_ms": derive_ms,
                "incremental_decision": derived.decision,
                "ledger_append_ms": append_ms,
                "ledger_verify_ms": verify_ms,
                "artifact_storage_bytes": dir_bytes(self.brain / "real-registry")
                + dir_bytes(self.brain / "real-ledger"),
            }
        )


# ──────────────────────────────────────────────
# Stage: pytest-derived reports
# ──────────────────────────────────────────────


def run_pytest(targets: List[str], label: str) -> Dict[str, Any]:
    """Run pytest and turn its JUnit report into a structured result."""
    with tempfile.TemporaryDirectory(prefix="brain-rc-junit-") as tmp:
        xml_path = Path(tmp) / "report.xml"
        argv = [
            sys.executable,
            "-m",
            "pytest",
            *targets,
            "-q",
            f"--junit-xml={xml_path}",
        ]
        started = time.perf_counter()
        completed = subprocess.run(
            argv, cwd=REPO_ROOT, capture_output=True, text=True, timeout=3600
        )
        duration = round(time.perf_counter() - started, 3)
        if not xml_path.is_file():
            raise RuntimeError(
                f"pytest produced no JUnit report for {label}:\n{completed.stdout[-4000:]}"
            )
        suite = ET.parse(xml_path).getroot().find("testsuite")
    assert suite is not None

    cases = []
    for case in suite.iter("testcase"):
        outcome = "passed"
        detail = ""
        for kind, name in (
            ("failure", "failed"),
            ("error", "error"),
            ("skipped", "skipped"),
        ):
            node = case.find(kind)
            if node is not None:
                outcome = name
                detail = (node.get("message") or "").strip()
                break
        cases.append(
            {
                "test": f"{case.get('classname')}::{case.get('name')}",
                "outcome": outcome,
                "duration_seconds": float(case.get("time") or 0.0),
                **({"detail": detail} if detail else {}),
            }
        )

    return {
        "label": label,
        "command": " ".join(["py", "-3", "-m", "pytest", *targets, "-q"]),
        "exit_code": completed.returncode,
        "duration_seconds": duration,
        "totals": {
            "tests": int(suite.get("tests") or 0),
            "passed": sum(1 for c in cases if c["outcome"] == "passed"),
            "failed": int(suite.get("failures") or 0),
            "errors": int(suite.get("errors") or 0),
            "skipped": int(suite.get("skipped") or 0),
        },
        "skipped_tests": [
            {"test": c["test"], "reason": c.get("detail", "")}
            for c in cases
            if c["outcome"] == "skipped"
        ],
        "failed_tests": [c for c in cases if c["outcome"] in ("failed", "error")],
        "tests": cases,
        "tail": completed.stdout.strip().splitlines()[-5:],
    }


# ──────────────────────────────────────────────
# Stage: manifest and narrative
# ──────────────────────────────────────────────

MANIFEST_ITEMS = [
    "feature-inventory.json",
    "registry-demo.json",
    "portfolio-demo.json",
    "incremental-demo.json",
    "freshness-demo.json",
    "workspace-demo.json",
    "experiment-demo.json",
    "patch-export-demo.json",
    "ledger-demo.json",
    "control-plane-demo.json",
    "security-checks.json",
    "api-smoke.json",
    "cli-smoke.json",
    "full-test-report.json",
    "performance.json",
    "known-limitations.md",
    "release-summary.md",
]


KNOWN_LIMITATIONS = """# v0.5.0 — known limitations

These are the limits of what the release-candidate evidence actually shows.
Nothing here is speculative: each item is either observed in the bundle or is
a property of the shipped code.

## Isolation

1. **Network isolation is unverified.** Validation child processes are not
   network-isolated by any operating-system mechanism on the host that produced
   this bundle. The runner reports `unverified`, and this bundle does not
   upgrade that claim.
2. **Filesystem isolation is a Git worktree plus path containment**, not a
   chroot, container, or jail. A validation command that deliberately escapes
   the workspace reaches the wider filesystem with the privileges of the
   invoking user.
3. **Process isolation is a new process group with timeout-driven
   termination.** A process that detaches from that group is no longer tracked
   and will not be killed by the supervisor.
4. **Environment isolation is an allowlist.** Variables outside it — including
   every secret-like name — are not passed to validation processes. This
   prevents accidental leakage, not a determined read of the filesystem.

## Analysis

5. **The Change Laboratory does not append ledger events.** `brain/lab` and
   `brain/experiments` perform the work; the orchestrating layer records it.
   A caller that drives the laboratory directly and never calls a recorder
   leaves no ledger trail.
6. **Option comparison is a fixed weighted formula, not a judgement.** It
   ranks what it can measure — findings, coupling, blast radius, changed
   lines, validation duration. It cannot weigh anything outside that list, and
   it is deliberately not a language model.
7. **Cross-repository relationships are derived from declared contracts and
   static imports.** Dynamic dispatch, reflection, configuration-driven wiring
   and network calls that do not appear in source are not seen.
8. **Incremental derivation falls back to a full rebuild** whenever the change
   set is too wide or the base generation is unusable. The fallback is correct
   but undoes the latency advantage.

## Measurements

9. **Every performance number is single-host and single-run**, except registry
   lookup, which is 200 repetitions reported as p50/p95. Percentiles appear
   only where repetition was practical. No capacity claim beyond the listed
   scales is supported.
10. **The real-repository workspace, patch and experiment timings are borrowed**
    from `reports/v0.5.0-engineering-lab/performance.json` rather than
    re-measured here, and are labelled as such in `performance.json`.

## Scope

11. **Project Brain never mutates an authoritative repository.** Candidate
    patches are applied only inside disposable managed workspaces. Exported
    patches are files for a human to review and apply.
12. **Autonomy classification is unchanged at L1.** This release adds no
    capability that acts on production, and none of its endpoints or CLIs can
    grant one.
"""


def build_manifest(bundle: Bundle) -> None:
    provenance = (
        json.loads(PROVENANCE.read_text(encoding="utf-8")) if PROVENANCE.is_file() else {}
    )
    missing = [name for name in MANIFEST_ITEMS if not (OUT_DIR / name).is_file()]
    if missing:
        raise SystemExit(
            "cannot build the manifest, these artifacts are missing: " + ", ".join(missing)
        )

    # A manifest entry without provenance is worse than no manifest: it looks
    # like a record and vouches for nothing. Say which stage has to be re-run.
    unprovenanced = [name for name in MANIFEST_ITEMS if name not in provenance]
    if unprovenanced:
        raise SystemExit(
            "these artifacts have no recorded provenance, re-run the stage that "
            "produces them: " + ", ".join(unprovenanced)
        )

    items = []
    for name in MANIFEST_ITEMS:
        path = OUT_DIR / name
        entry = provenance[name]
        items.append(
            {
                "path": f"reports/v0.5.0-release-candidate/{name}",
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
                "generation_command": entry["generation_command"],
                "git_revision": entry["git_revision"],
                "generated_at_utc": entry["generated_at_utc"],
                "redaction_status": entry["redaction_status"],
            }
        )

    manifest = {
        "release_candidate": RC_NAME,
        "milestone": MILESTONE,
        "autonomy_classification": "L1 — unchanged by this release",
        "git_revision": bundle.revision,
        "generated_from": (
            "the working tree at git_revision, including the release-candidate "
            "tooling that is committed as the release-candidate commit itself"
        ),
        "created_at_utc": utc_now(),
        "non_autonomy_statement": NON_AUTONOMY_STATEMENT,
        "checksum_note": (
            "The manifest's own checksum is in manifest.sha256, alongside this "
            "file — never inside it."
        ),
        "item_count": len(items),
        "items": items,
    }
    path = OUT_DIR / "manifest.json"
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (OUT_DIR / "manifest.sha256").write_text(
        f"{sha256_file(path)}  manifest.json\n", encoding="utf-8", newline="\n"
    )
    print("  wrote manifest.json")
    print("  wrote manifest.sha256")


def verify_release_gates() -> int:
    """RC Phase 5 gates, as checks rather than claims.

    Reads only — it must never change a byte of the bundle, because the manifest
    has already vouched for every checksum by the time this runs.
    """
    failures: List[str] = []
    passes: List[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        (passes if ok else failures).append(f"{name}{': ' + detail if detail else ''}")
        print(f"  [{'ok ' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}")

    # ── the manifest vouches for what is actually on disk ──
    manifest = json.loads((OUT_DIR / "manifest.json").read_text(encoding="utf-8"))
    items = manifest["items"]
    check("manifest covers every artifact", len(items) == len(MANIFEST_ITEMS), f"{len(items)} items")

    bad_sums = [
        i["path"] for i in items if sha256_file(REPO_ROOT / i["path"]) != i["sha256"]
    ]
    check("every recorded checksum matches the file", not bad_sums, ", ".join(bad_sums))

    incomplete = [
        i["path"]
        for i in items
        for field in ("generation_command", "git_revision", "generated_at_utc", "redaction_status")
        if not i.get(field) or i[field] == "unknown"
    ]
    check("every item carries full provenance", not incomplete, ", ".join(sorted(set(incomplete))))

    # The checksums have to survive a checkout, not just this working tree:
    # .gitattributes normalises line endings, so a CRLF artifact would hash
    # differently for whoever verifies the bundle after cloning.
    stored_mismatch = []
    for item in items:
        blob = subprocess.run(
            ["git", "show", f"HEAD:{item['path']}"],
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=120,
        )
        if blob.returncode != 0:
            continue  # not committed yet; the working-tree check above stands
        if hashlib.sha256(blob.stdout).hexdigest() != item["sha256"]:
            stored_mismatch.append(item["path"])
    check(
        "committed bytes match the recorded checksums",
        not stored_mismatch,
        ", ".join(stored_mismatch),
    )

    manifest_path = OUT_DIR / "manifest.json"
    own_sum = sha256_file(manifest_path)
    recorded = (OUT_DIR / "manifest.sha256").read_text(encoding="utf-8").split()[0]
    check("manifest.sha256 matches manifest.json", recorded == own_sum)
    check(
        "manifest does not contain its own checksum",
        own_sum not in manifest_path.read_text(encoding="utf-8"),
    )
    check(
        "manifest omits itself and its checksum file",
        not any(Path(i["path"]).name.startswith("manifest.") for i in items),
    )

    # ── nothing in the bundle that must not ship ──
    bundle_files = [p for p in OUT_DIR.rglob("*") if p.is_file()]
    check("no .git directory inside the report bundle", not list(OUT_DIR.rglob(".git")))
    check(
        "no runtime databases or caches inside the bundle",
        not [
            p
            for p in bundle_files
            if p.suffix in {".db", ".sqlite", ".sqlite3", ".pyc"} or "__pycache__" in p.parts
        ],
    )
    check(
        "bundle holds exactly the manifested artifacts plus the manifest",
        sorted(p.name for p in bundle_files)
        == sorted(MANIFEST_ITEMS + ["manifest.json", "manifest.sha256"]),
    )

    host_markers = {
        "repository root": str(REPO_ROOT),
        "home directory": str(Path.home()),
        "temp directory": str(tempfile.gettempdir()),
    }
    username = os.environ.get("USERNAME", "")
    if username:
        host_markers["user name"] = username
    leaks = []
    for path in bundle_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, marker in host_markers.items():
            if marker and marker.lower() in text.lower():
                leaks.append(f"{path.name} ({label})")
    check("no absolute local paths in the bundle", not leaks, ", ".join(sorted(set(leaks))))

    # Secrets are compared, never printed: only the variable name is reported.
    env_file = REPO_ROOT / ".env"
    exposed = []
    skipped = []
    if env_file.is_file():
        joined = "\n".join(
            p.read_text(encoding="utf-8", errors="replace") for p in bundle_files
        )
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if len(value) < 8:
                continue
            # A short all-lowercase dictionary word cannot be told apart from
            # ordinary prose — "postgres" matches a test name. Scanning for it
            # produces noise that would hide a real leak, so it is excluded and
            # named here rather than silently folded into a pass.
            if len(value) < 12 and value.isalpha() and value.islower():
                skipped.append(key)
                continue
            if value in joined:
                exposed.append(key)
    detail = ", ".join(exposed)
    if skipped and not exposed:
        detail = f"low-entropy values not scanned: {', '.join(skipped)}"
    check("no .env secret value appears in the bundle", not exposed, detail)

    # ── no leaked laboratory state in the authoritative checkout ──
    ws_records = REPO_ROOT / ".brain" / "workspace" / "workspaces.json"
    if ws_records.is_file():
        records = json.loads(ws_records.read_text(encoding="utf-8"))
        live = [
            f"{k}:{v.get('state')}"
            for k, v in records.items()
            if v.get("state") in {"ready", "active", "validating", "quarantined"}
        ]
        check("no active or quarantined managed workspace", not live, ", ".join(live))
    else:
        check("no managed workspace state in the authoritative checkout", True)
    managed_root = REPO_ROOT / ".brain" / "workspaces"
    check(
        "no disposable workspace directory left behind",
        not managed_root.exists() or not any(managed_root.iterdir()),
    )

    # ── the authoritative checkout is where it should be ──
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout.strip()
    check("authoritative working tree is clean", not status, status.replace("\n", " | "))

    worktrees = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    temp_root = Path(tempfile.gettempdir()).resolve()
    stray = []
    for line in worktrees.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line.split(" ", 1)[1]).resolve()
        if path == REPO_ROOT:
            continue
        if temp_root in path.parents or REPO_ROOT in path.parents:
            stray.append(str(path.name))
    check("no temporary Git fixture worktree registered", not stray, ", ".join(stray))

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    if failures:
        print("RELEASE GATES NOT MET")
        return 1
    print("all release gates met")
    return 0


def build_release_summary(bundle: Bundle) -> None:
    def load(name: str) -> Dict[str, Any]:
        path = OUT_DIR / name
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    inventory = load("feature-inventory.json")
    registry = load("registry-demo.json")
    portfolio = load("portfolio-demo.json")
    incremental = load("incremental-demo.json")
    freshness = load("freshness-demo.json")
    workspace = load("workspace-demo.json")
    experiment = load("experiment-demo.json")
    export = load("patch-export-demo.json")
    ledger = load("ledger-demo.json")
    control = load("control-plane-demo.json")
    security = load("security-checks.json")
    api = load("api-smoke.json")
    cli = load("cli-smoke.json")
    full = load("full-test-report.json")
    perf = load("performance.json")

    totals = inventory.get("totals", {})
    checks = inventory.get("reference_checks", {})
    small = (perf.get("scales") or {}).get("small_fixture_portfolio", {})
    real = (perf.get("scales") or {}).get("real_project_brain_repository", {})
    medium = (perf.get("scales") or {}).get("medium_generated_portfolio", {})

    lines = [
        f"# {RC_NAME} — release summary",
        "",
        f"- Milestone: {MILESTONE}",
        f"- Git revision: `{bundle.revision}`",
        f"- Generated: {utc_now()}",
        "- Autonomy classification: **L1, unchanged**",
        "",
        "## Non-autonomy statement",
        "",
        "```text",
        NON_AUTONOMY_STATEMENT,
        "```",
        "",
        "## What was verified",
        "",
        f"- **Registry** — {registry.get('repository_count', 0)} repositories registered "
        f"through the shipped store; duplicate registration {registry.get('duplicate_registration', 'n/a')}; "
        f"integrity check valid: {registry.get('integrity', {}).get('valid')}.",
        f"- **Portfolio** — cross-repository graph built and activated "
        f"(status `{portfolio.get('status')}`) over "
        f"{len(portfolio.get('portfolio', {}).get('repositories', []))} repositories with "
        f"{portfolio.get('declared_contracts', 0)} declared contracts.",
        f"- **Incremental intelligence** — decision `{incremental.get('decision')}` for the "
        f"api-service contract change; derived generation "
        f"`{(incremental.get('derivation') or {}).get('new_generation_id', 'n/a')}`.",
        f"- **Freshness** — {freshness.get('tracked_artifacts', 0)} artifacts tracked; the "
        f"shared-contract break marked {len(freshness.get('stale_artifacts', []))} stale "
        f"({', '.join(freshness.get('impacted_by_shared_contract_change', []))}).",
        f"- **Change Laboratory** — {len(workspace.get('sessions', []))} disposable workspaces "
        f"created, patched, validated and cleaned; directories removed: "
        f"{workspace.get('directories_removed')}.",
        f"- **Experiment** — conclusion `{experiment.get('conclusion')}`, recommended option "
        f"`{experiment.get('recommended_option_id')}`, compared by "
        f"{experiment.get('compared_by')}.",
        f"- **Patch export** — export before human acceptance: "
        f"{export.get('export_before_human_acceptance')}; after acceptance the patch was "
        f"exported and **not** applied "
        f"(authoritative source unchanged: "
        f"{export.get('authoritative_source_still_contains_original_import')}).",
        f"- **Evidence ledger** — {ledger.get('events_checked', 0)} events, chain valid: "
        f"{ledger.get('valid')}.",
        f"- **Control plane** — active workspaces: {control.get('active_workspaces')}, "
        f"quarantined: {control.get('quarantined_workspaces')}, health: "
        f"`{(control.get('health') or {}).get('status')}`.",
        f"- **Security checks** — {security.get('passed', 0)} passed, "
        f"{security.get('failed', 0)} failed.",
        "",
        "## Tests",
        "",
    ]

    for report, title in ((api, "API smoke"), (cli, "CLI smoke"), (full, "Full regression")):
        counts = report.get("totals", {})
        lines.append(
            f"- **{title}** — `{report.get('command', 'n/a')}`: "
            f"{counts.get('passed', 0)} passed, {counts.get('failed', 0)} failed, "
            f"{counts.get('errors', 0)} errors, {counts.get('skipped', 0)} skipped "
            f"in {report.get('duration_seconds', 0)}s (exit {report.get('exit_code')})."
        )

    skips = full.get("skipped_tests", [])
    if skips:
        lines += ["", "Every skip in the full regression, with its reason:", ""]
        lines += [f"- `{s['test']}` — {s['reason'] or 'no reason recorded'}" for s in skips]

    lines += [
        "",
        "## Feature inventory",
        "",
        f"- Derived from the running code: {totals.get('subsystems', 'n/a')} subsystems, "
        f"{totals.get('modules', 'n/a')} modules, {totals.get('classes', 'n/a')} classes, "
        f"{totals.get('api_endpoints', 'n/a')} API endpoints and "
        f"{totals.get('cli_commands', 'n/a')} CLI commands across "
        f"{totals.get('cli_modules', 'n/a')} CLI modules (see `feature-inventory.json`).",
        f"- Unreferenced modules: {len(checks.get('unreferenced_modules', []))}; "
        f"endpoints without a test mention: "
        f"{len(checks.get('endpoints_not_mentioned_in_tests', []))}; "
        f"unresolved claimed symbols: "
        f"{len(checks.get('unresolved_claimed_symbols', []))}.",
        "",
        "## Performance",
        "",
        "| Measurement | Small fixture portfolio | Medium generated portfolio | "
        "Real Project Brain |",
        "| --- | --- | --- | --- |",
        f"| Registry lookup p50 / p95 (ms) | "
        f"{small.get('registry_lookup', {}).get('p50_ms')} / "
        f"{small.get('registry_lookup', {}).get('p95_ms')} | "
        f"{medium.get('registry_lookup', {}).get('p50_ms')} / "
        f"{medium.get('registry_lookup', {}).get('p95_ms')} | "
        f"{real.get('registry_lookup', {}).get('p50_ms')} / "
        f"{real.get('registry_lookup', {}).get('p95_ms')} |",
        f"| Full repository graph rebuild (ms) | "
        f"{small.get('repository_graph_full_build_ms')} | "
        f"{medium.get('repository_graph_full_build_ms')} | "
        f"{real.get('repository_graph_full_build_ms')} |",
        f"| Portfolio build (ms) | {small.get('portfolio_build_ms')} | "
        f"{medium.get('portfolio_build_ms')} | n/a (single repository) |",
        f"| Incremental plan / derive (ms) | {small.get('incremental_plan_ms')} / "
        f"{small.get('incremental_derive_ms')} | n/a | "
        f"{real.get('incremental_plan_ms')} / {real.get('incremental_derive_ms')} |",
        f"| Ledger append / verify (ms) | {small.get('ledger_append_ms')} / "
        f"{small.get('ledger_verify_ms')} | n/a | {real.get('ledger_append_ms')} / "
        f"{real.get('ledger_verify_ms')} |",
        f"| Artifact storage (bytes) | {small.get('artifact_storage_bytes')} | "
        f"{medium.get('artifact_storage_bytes')} | {real.get('artifact_storage_bytes')} |",
        "",
        "Workspace, patch-application, validation and experiment timings for the "
        "real repository are carried over from the engineering-lab run and are "
        "labelled as such in `performance.json`.",
        "",
        "## Cleanup",
        "",
        "- Every disposable workspace created by this bundle was removed; the control "
        f"plane reports {control.get('active_workspaces')} active and "
        f"{control.get('quarantined_workspaces')} quarantined workspaces.",
        "- All fixture Git repositories and runtime state were created under a "
        "temporary directory and deleted when the run finished.",
        "- No candidate patch was applied to the authoritative checkout, and no "
        "commit, branch, push or deployment was made by this run.",
        "- One write to the authoritative checkout did happen, by design: measuring "
        "a real graph rebuild writes generations into the repository's own "
        "gitignored `.brain/` metadata directory. That is what the "
        "`write_brain_metadata` capability exists for; no tracked file was touched.",
        "",
        "## Known limitations",
        "",
        "See `known-limitations.md`. The two that matter most: network isolation is "
        "**unverified**, and option comparison is a fixed weighted formula rather "
        "than a judgement.",
        "",
    ]
    bundle.write_text("release-summary.md", "\n".join(lines))


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build the v0.5.0 release-candidate bundle")
    parser.add_argument(
        "--stage",
        required=True,
        choices=[
            "demos",
            "inventory",
            "api-smoke",
            "cli-smoke",
            "full-tests",
            "manifest",
            "verify",
        ],
    )
    args = parser.parse_args(argv)
    command = f"py -3 scripts/v050_release_candidate.py --stage {args.stage}"

    work = Path(tempfile.mkdtemp(prefix="brain-v050-rc-"))
    bundle = Bundle(OUT_DIR, work, command)
    try:
        if args.stage == "demos":
            Demos(bundle, work).run()
        elif args.stage == "inventory":
            # The inventory is derived by its own script; this stage runs it and
            # takes responsibility for its provenance and redaction.
            subprocess.run(
                [sys.executable, "scripts/v050_feature_inventory.py"],
                cwd=REPO_ROOT,
                check=True,
                timeout=900,
            )
            bundle.adopt("feature-inventory.json", INVENTORY_COMMAND)
        elif args.stage == "api-smoke":
            bundle.write(
                "api-smoke.json", run_pytest(["tests/test_v050_api_contracts.py"], "API smoke")
            )
        elif args.stage == "cli-smoke":
            bundle.write(
                "cli-smoke.json", run_pytest(["tests/test_v050_cli_contracts.py"], "CLI smoke")
            )
        elif args.stage == "full-tests":
            bundle.write("full-test-report.json", run_pytest(["tests/"], "Full regression"))
        elif args.stage == "manifest":
            bundle.write_text("known-limitations.md", KNOWN_LIMITATIONS)
            build_release_summary(bundle)
            build_manifest(bundle)
        elif args.stage == "verify":
            rc = verify_release_gates()
            if rc:
                return rc
    finally:
        # ignore_errors would leave the fixture repositories behind: Git marks
        # its object files read-only, and Windows refuses to delete those.
        shutil.rmtree(work, onerror=_force_remove)
        if work.exists():
            print(f"WARNING: temporary fixture directory not removed: {work}")
    print(f"Stage '{args.stage}' complete -> {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
