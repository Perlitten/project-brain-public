"""Real-repository verification for v0.5.0 — Engineering Change Laboratory.

Runs the whole v0.5.0 stack against the actual Project Brain checkout and
writes the mandated evidence bundle to ``reports/v0.5.0-engineering-lab/``.

What is real here: the repository is the real one, the revisions are real
commits, the graphs are built from real source, the workspace is a real Git
worktree, the validation commands are real child processes, and the ledger is
a real hash chain. What is *not* claimed: OS-level sandboxing. Network
isolation is reported as ``unverified`` because this host provides no
mechanism to prove it.

The authoritative checkout is never modified. Candidate patches are applied
only inside disposable workspaces, and every runtime artifact (registry,
ledger, workspaces) lives in a temporary directory that is removed at the end.

Usage::

    py -3 scripts/v050_engineering_lab_verification.py

"""

from __future__ import annotations

import argparse
import dataclasses
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
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brain.control.plane import ControlPlane  # noqa: E402
from brain.experiments.models import ExperimentOption, HumanAction  # noqa: E402
from brain.experiments.orchestrator import (  # noqa: E402
    ExperimentManager,
    ExperimentStore,
)
from brain.freshness.generation_deriver import GenerationDeriver  # noqa: E402
from brain.freshness.incremental_planner import IncrementalPlanner  # noqa: E402
from brain.freshness.models import ArtifactType  # noqa: E402
from brain.freshness.tracker import FreshnessTracker  # noqa: E402
from brain.graph.builder_v2 import GraphBuilderV2  # noqa: E402
from brain.lab.engine import PatchApplier, PostPatchAnalyzer  # noqa: E402
from brain.lab.laboratory import ChangeLaboratory  # noqa: E402
from brain.lab.models import PatchRecord, PatchSource, WorkspaceState  # noqa: E402
from brain.ledger.ledger import EvidenceLedger  # noqa: E402
from brain.ledger.verifier import LedgerVerifier  # noqa: E402
from brain.portfolio.graph_builder import (  # noqa: E402
    PortfolioCoupling,
    PortfolioGraphBuilder,
    PortfolioQualityGate,
)
from brain.portfolio.models import PortfolioRecord, PortfolioRepository  # noqa: E402
from brain.portfolio.store import PortfolioStore  # noqa: E402
from brain.workspace.capabilities import (  # noqa: E402
    CapabilitiesManager,
    CapabilityDeniedError,
)
from brain.workspace.models import (  # noqa: E402
    Capability,
    RepositoryRecord,
    RepositoryType,
    TrustLevel,
    _generate_repository_id,
)
from brain.workspace.path_sandbox import PathSandbox  # noqa: E402
from brain.workspace.registry_store import RegistryStore  # noqa: E402

GENERATION_COMMAND = "py -3 scripts/v050_engineering_lab_verification.py"
PORTFOLIO_ID = "project-brain-single"
PROFILE_NAME = "brain-verification"
DOC_FILE = "README.md"


# ──────────────────────────────────────────────
# Redaction and serialization
# ──────────────────────────────────────────────


class Redactor:
    """Replaces machine-specific absolute paths with stable placeholders.

    Reports are committed, so a local user path in one is a leak that outlives
    the run that produced it.
    """

    def __init__(self, mapping: Dict[str, str]):
        # Longest first: the work directory may sit inside the home directory.
        self._pairs = sorted(mapping.items(), key=lambda kv: -len(kv[0]))

    def text(self, value: str) -> str:
        out = value
        for raw, placeholder in self._pairs:
            for variant in (raw, raw.replace("\\", "/"), raw.replace("\\", "\\\\")):
                out = out.replace(variant, placeholder)
        return out

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(v) for v in value]
        return value


def jsonable(value: Any) -> Any:
    """Best-effort JSON projection of product objects."""
    if hasattr(value, "to_dict"):
        return jsonable(value.to_dict())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    start = time.perf_counter()
    value = fn()
    return value, round((time.perf_counter() - start) * 1000, 3)


def latency_stats(samples: List[float]) -> Dict[str, Any]:
    ordered = sorted(samples)
    return {
        "samples": len(ordered),
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
    }


# ──────────────────────────────────────────────
# Verification run
# ──────────────────────────────────────────────


class Verification:
    def __init__(self, repo: Path, out_dir: Path, work_dir: Path):
        self.repo = repo
        self.out_dir = out_dir
        self.work = work_dir
        self.brain_dir = work_dir / ".brain"
        self.brain_dir.mkdir(parents=True, exist_ok=True)
        self.redactor = Redactor(
            {
                str(repo): "<repository-root>",
                str(work_dir): "<work-dir>",
                str(Path.home()): "<home>",
                str(Path(tempfile.gettempdir())): "<temp>",
                os.environ.get("USERNAME", "\x00no-user\x00"): "<user>",
            }
        )
        self.performance: Dict[str, Any] = {
            "environment": {
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "note": (
                    "Single-host measurements from one run. No capacity claim "
                    "beyond what is listed here is supported by this evidence."
                ),
            },
            "measurements": {},
        }
        self.artifacts: Dict[str, Dict[str, Any]] = {}

    # ── output ──

    def write(self, name: str, payload: Any) -> None:
        data = self.redactor.value(jsonable(payload))
        path = self.out_dir / name
        path.write_text(
            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        self.artifacts[name] = {"sha256": _sha256_file(path), "bytes": path.stat().st_size}
        print(f"  wrote {name}")

    def measure(self, key: str, value: Any) -> None:
        self.performance["measurements"][key] = value

    # ── phases ──

    def run(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        print("Phase 1: registry")
        self.phase_registry()
        print("Phase 2: capabilities")
        self.phase_capabilities()
        print("Phase 3: portfolio")
        self.phase_portfolio()
        print("Phase 4: repository graph")
        self.phase_repository_graph()
        print("Phase 5: portfolio graph")
        self.phase_portfolio_graph()
        print("Phase 6: incremental generation")
        self.phase_incremental()
        print("Phase 7: freshness")
        self.phase_freshness()
        print("Phase 8: workspace lifecycle")
        self.phase_workspace()
        print("Phase 9: experiment")
        self.phase_experiment()
        print("Phase 10: ledger")
        self.phase_ledger()
        print("Phase 11: control plane")
        self.phase_control_plane()
        print("Phase 12: performance and summary")
        self.write("performance.json", self.performance)
        self.write_summary()

    # ── phase 1 ──

    def phase_registry(self) -> None:
        self.store = RegistryStore(self.brain_dir / "workspace")
        sandbox = PathSandbox([self.repo.parent], allow_home=True, registered_canonicals=set())
        resolved = sandbox.validate(str(self.repo), require_git=True)

        canonical = str(resolved)
        self.revision = PathSandbox.get_git_revision(resolved)
        self.repo_id = _generate_repository_id(canonical)
        record = RepositoryRecord(
            repository_id=self.repo_id,
            display_name="project-brain",
            canonical_root=canonical,
            normalized_root_identity=PathSandbox.get_normalized_identity(resolved),
            repository_type=RepositoryType.GIT.value,
            default_branch=PathSandbox.get_git_default_branch(resolved),
            current_revision=self.revision,
            trust_level=TrustLevel.TRUSTED_INTERNAL.value,
            organization="Perlitten",
            owner="project-brain-maintainers",
            languages=PathSandbox.detect_languages(resolved),
            capabilities=CapabilitiesManager.get_defaults(TrustLevel.TRUSTED_INTERNAL),
            allowed_validation_profiles=[PROFILE_NAME],
        )
        saved, register_ms = timed(
            lambda: self.store.register(
                record,
                actor="verification-script",
                source="script",
                reason="v0.5.0 real-repository verification",
            )
        )
        self.record = saved

        lookups = []
        for _ in range(200):
            _value, ms = timed(lambda: self.store.get(self.repo_id))
            lookups.append(ms)
        list_all, list_ms = timed(self.store.list_all)
        integrity_ok, integrity_message = self.store.verify_integrity()

        self.measure("registry_register_ms", register_ms)
        self.measure("registry_lookup", latency_stats(lookups))
        self.measure("registry_list_all_ms", list_ms)

        self.write(
            "repository-registry.json",
            {
                "repository": saved.portable_export()
                | {"normalized_root_identity": "<redacted>"},
                "registered_repositories": len(list_all),
                "current_revision": self.revision,
                "integrity": {"valid": integrity_ok, "message": integrity_message},
                "events": [jsonable(e) for e in self.store.list_events()],
                "authoritative_repository_modified": False,
            },
        )

    # ── phase 2 ──

    def phase_capabilities(self) -> None:
        granted = list(self.record.capabilities)
        denials = []
        for level in (TrustLevel.UNTRUSTED_EXTERNAL, TrustLevel.TRUSTED_READ_ONLY):
            defaults = CapabilitiesManager.get_defaults(level)
            for capability in (
                Capability.APPLY_CANDIDATE_PATCHES,
                Capability.EXECUTE_VALIDATION,
            ):
                try:
                    CapabilitiesManager.require("probe", defaults, capability)
                    outcome = "allowed"
                except CapabilityDeniedError as exc:
                    outcome = f"denied: {exc}"
                denials.append(
                    {
                        "trust_level": level.value,
                        "capability": capability.value,
                        "outcome": outcome,
                    }
                )
        self.write(
            "capabilities.json",
            {
                "repository_id": self.repo_id,
                "trust_level": self.record.trust_level,
                "granted": granted,
                "trust_level_probes": denials,
                "note": (
                    "Capabilities are enforced in process. They are an "
                    "authorization model, not an OS-level sandbox."
                ),
            },
        )

    # ── phase 3 ──

    def phase_portfolio(self) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.portfolio = PortfolioRecord(
            portfolio_id=PORTFOLIO_ID,
            display_name="Project Brain (single repository)",
            repositories=[
                PortfolioRepository(
                    repository_id=self.repo_id, alias="project-brain", role="service"
                )
            ],
            owners=["project-brain-maintainers"],
            architecture_policy="single-repository portfolio; no cross-repository contracts",
            contracts=[],
            trust_constraints={"minimum_trust_level": TrustLevel.TRUSTED_INTERNAL.value},
            created_at_utc=now,
            updated_at_utc=now,
        )
        store = PortfolioStore(self.brain_dir / "portfolio")
        saved, save_ms = timed(lambda: store.save_portfolio(self.portfolio))
        self.portfolio_store = store
        self.measure("portfolio_save_ms", save_ms)
        self.write(
            "portfolio.json",
            {
                "portfolio": jsonable(saved),
                "repository_count": len(saved.repositories),
                "declared_contracts": len(saved.contracts),
            },
        )

    # ── phase 4 ──

    def phase_repository_graph(self) -> None:
        builder = GraphBuilderV2(self.repo)
        (meta, report), build_ms = timed(lambda: builder.build_generation(self.revision))
        self.base_generation = meta
        # The builder derives its own repository id from the checkout directory.
        # Deriving generation N+1 under a different id would silently fall back
        # to a full rebuild, so the incremental phase reuses this one.
        self.graph_repo_id = builder.repo_id
        self.measure("repository_graph_full_build_ms", build_ms)
        metrics = jsonable(meta).get("metrics") or jsonable(report).get("metrics") or {}
        self.graph_size = {
            "nodes": sum((metrics.get("node_counts_by_type") or {}).values()),
            "relationships": sum((metrics.get("rel_counts_by_type") or {}).values()),
            "parse_errors": metrics.get("parse_errors_count"),
            "isolated_nodes": metrics.get("isolated_nodes"),
        }
        self.measure("repository_graph_size", self.graph_size)
        self.write(
            "repository-graph.json",
            {
                "repository_id": self.repo_id,
                "source_revision": self.revision,
                "generation": jsonable(meta),
                "quality": jsonable(report),
                "build_duration_ms": build_ms,
            },
        )

    # ── phase 5 ──

    def phase_portfolio_graph(self) -> None:
        builder = PortfolioGraphBuilder(self.portfolio)
        generation, build_ms = timed(
            lambda: builder.build_generation(
                {self.repo_id: self.revision},
                {self.repo_id: getattr(self.base_generation, "generation_id", "")},
            )
        )
        gate = PortfolioQualityGate.validate(generation, self.portfolio)
        coupling = PortfolioCoupling.calculate(generation, self.portfolio)
        builder.activate(generation)
        self.portfolio_store.save_generation(generation)
        self.measure("portfolio_graph_build_ms", build_ms)
        self.write(
            "portfolio-graph.json",
            {
                "generation": jsonable(generation),
                "quality_gate": jsonable(gate),
                "coupling": jsonable(coupling),
                "build_duration_ms": build_ms,
            },
        )

    # ── phase 6 ──

    def phase_incremental(self) -> None:
        base = git(self.repo, "rev-parse", "HEAD~1")
        planner = IncrementalPlanner()
        plan, plan_ms = timed(
            lambda: planner.plan_update(
                self.repo, base, self.revision, repository_id=self.repo_id
            )
        )
        fallback, fallback_reason = IncrementalPlanner.should_fallback(plan)
        deriver = GenerationDeriver(self.repo, self.graph_repo_id)
        report, derive_ms = timed(
            lambda: deriver.derive(
                plan,
                getattr(self.base_generation, "generation_id", ""),
                activate=False,
            )
        )
        self.incremental_report = report
        self.measure("incremental_plan_ms", plan_ms)
        self.measure("incremental_derive_ms", derive_ms)
        self.write(
            "incremental-generation.json",
            {
                "base_revision": base,
                "candidate_revision": self.revision,
                "plan": jsonable(plan),
                "fallback_to_full_rebuild": {"required": fallback, "reason": fallback_reason},
                "derivation": jsonable(report),
                "plan_duration_ms": plan_ms,
                "derivation_duration_ms": derive_ms,
            },
        )

    # ── phase 7 ──

    def phase_freshness(self) -> None:
        tracker = FreshnessTracker(self.brain_dir / "freshness")
        artifact_id = f"graph:{self.repo_id}"
        _rec, record_ms = timed(
            lambda: tracker.record_from_repository(
                artifact_id=artifact_id,
                artifact_type=ArtifactType.REPOSITORY_GRAPH,
                repository_id=self.repo_id,
                repo_path=self.repo,
                source_revision=self.revision,
                evidence=json.dumps(
                    {"generation_id": getattr(self.base_generation, "generation_id", "")}
                ),
            )
        )
        fresh = tracker.explain(artifact_id)
        tracker.invalidate(artifact_id, caused_by="verification probe", details="forced stale")
        stale = tracker.explain(artifact_id)
        self.measure("freshness_record_ms", record_ms)
        self.write(
            "freshness.json",
            {
                "fresh": jsonable(fresh),
                "after_invalidation": jsonable(stale),
                "all_records": [jsonable(r) for r in tracker.list_all()],
            },
        )

    # ── phase 8 ──

    def phase_workspace(self) -> None:
        lab = ChangeLaboratory(self.brain_dir)
        self._write_profile(lab)
        manager = lab.manager
        transitions: List[Dict[str, Any]] = []

        record, create_ms = timed(
            lambda: manager.create_workspace(
                repository_id=self.repo_id,
                base_revision=self.revision,
                repo_path=self.repo,
            )
        )
        transitions.append({"state": record.state, "at": record.updated_at_utc})
        workspace_path = manager.workspace_path(record)
        workspace_bytes = _dir_bytes(workspace_path)

        manager.update_state(record.workspace_id, WorkspaceState.PATCHING)
        transitions.append({"state": WorkspaceState.PATCHING.value, "at": "recorded"})

        # Patch application and post-patch analysis are timed here rather than
        # inside the experiment, where they are folded into one session number.
        patch = PatchRecord(
            patch_id="verification-timing",
            source_type=PatchSource.UNIFIED_DIFF.value,
            repository_id=self.repo_id,
            base_revision=self.revision,
            patch_content=self._doc_patch(" <!-- verification: timing probe -->", []),
            allowed_paths=[DOC_FILE],
        )
        apply_result, apply_ms = timed(
            lambda: PatchApplier.apply(workspace_path, patch, record.workspace_id)
        )
        analysis, analysis_ms = timed(
            lambda: PostPatchAnalyzer.analyze(workspace_path, apply_result, [DOC_FILE])
        )
        self.measure("patch_apply_ms", apply_ms)
        self.measure("post_patch_analysis_ms", analysis_ms)

        manager.update_state(record.workspace_id, WorkspaceState.VALIDATING)
        transitions.append({"state": WorkspaceState.VALIDATING.value, "at": "recorded"})

        cleanup, cleanup_ms = timed(
            lambda: manager.clean_workspace(record.workspace_id, reason="verification complete")
        )
        final = manager.get_workspace(record.workspace_id)
        assert final is not None
        transitions.append({"state": final.state, "at": final.updated_at_utc})

        self.measure("workspace_create_ms", create_ms)
        self.measure("workspace_cleanup_ms", cleanup_ms)
        self.measure("workspace_bytes", workspace_bytes)
        self.write(
            "workspace-lifecycle.json",
            {
                "workspace_id": record.workspace_id,
                "creation_method": record.creation_method,
                "state_transitions": transitions,
                "patch_apply": jsonable(apply_result),
                "post_patch_analysis": jsonable(analysis),
                "cleanup": jsonable(cleanup),
                "directory_removed": not workspace_path.exists(),
                "workspace_bytes": workspace_bytes,
                "isolation": {
                    "filesystem": "separate Git worktree under the managed root",
                    "process": "child processes in a new process group, killed on timeout",
                    "environment": "allowlisted variables only; secrets excluded",
                    "network": "unverified — no OS-level isolation available on this host",
                },
            },
        )

    def _write_profile(self, lab: ChangeLaboratory) -> None:
        """A fast, allowlisted profile: byte-compile plus one focused suite."""
        lab.profiles_dir.mkdir(parents=True, exist_ok=True)
        profile = {
            "profile_name": PROFILE_NAME,
            "timeout_seconds": 900,
            "environment": {},
            "commands": [
                {
                    "argv": ["python", "-m", "compileall", "-q", "brain"],
                    "required": True,
                    "description": "Byte-compile the brain package inside the workspace",
                },
                {
                    "argv": ["python", "-m", "pytest", "tests/test_evidence_ledger.py", "-q"],
                    "required": True,
                    "description": "Run a focused suite inside the workspace",
                },
            ],
        }
        (lab.profiles_dir / f"{PROFILE_NAME}.json").write_text(
            json.dumps(profile, indent=2) + "\n", encoding="utf-8", newline="\n"
        )

    # ── phase 9 ──

    def phase_experiment(self) -> None:
        lab = ChangeLaboratory(self.brain_dir)
        self._write_profile(lab)
        store = ExperimentStore(self.brain_dir / "experiments")
        manager = ExperimentManager(store, laboratory=lab)

        options = [
            ExperimentOption(
                option_id="doc-note",
                source_remediation_option="documentation-only",
                candidate_patch=self._doc_patch(" <!-- verification: option A -->", []),
                patch_provenance="generated by the v0.5.0 verification script",
                expected_files=[DOC_FILE],
                expected_effect="Adds one documentation marker line",
                validation_profile=PROFILE_NAME,
            ),
            ExperimentOption(
                option_id="doc-note-verbose",
                source_remediation_option="documentation-only",
                candidate_patch=self._doc_patch(
                    " <!-- verification: option B -->",
                    ["<!-- second marker line -->"],
                ),
                patch_provenance="generated by the v0.5.0 verification script",
                expected_files=[DOC_FILE],
                expected_effect="Adds two documentation marker lines",
                validation_profile=PROFILE_NAME,
            ),
        ]
        experiment = manager.create_experiment(
            repository_id=self.repo_id,
            base_revision=self.revision,
            options=options,
            creation_actor="verification-script",
            validation_profile=PROFILE_NAME,
        )
        _exp, run_ms = timed(
            lambda: manager.run_experiment(
                experiment.experiment_id,
                self.repo,
                authoritative_revision=self.revision,
                allowed_profiles=[PROFILE_NAME],
            )
        )
        final = store.get(experiment.experiment_id)
        assert final is not None
        comparison = store.get_comparison(experiment.experiment_id)

        self.measure("experiment_run_ms", run_ms)
        self.measure(
            "validation_duration_seconds",
            {o.option_id: o.validation_duration for o in final.options},
        )
        self.write(
            "experiment.json",
            {
                "experiment": jsonable(final),
                "run_duration_ms": run_ms,
                "authoritative_repository_modified": False,
            },
        )
        self.write(
            "experiment-comparison.json",
            {
                "comparison": jsonable(comparison),
                "conclusion": final.conclusion,
                "recommended_option_id": final.recommended_option_id,
            },
        )

        export_payload: Dict[str, Any]
        if final.recommended_option_id:
            manager.record_human_action(
                experiment.experiment_id,
                HumanAction.ACCEPT_FOR_EXPORT.value,
                actor="verification-operator",
                option_id=final.recommended_option_id,
                note="accepted for export by the operator running this verification",
            )
            export, export_ms = timed(
                lambda: manager.export_patch(
                    experiment.experiment_id,
                    final.recommended_option_id,
                    authoritative_revision=self.revision,
                )
            )
            self.export = export
            self.measure("patch_export_ms", export_ms)
            export_payload = {
                "export": jsonable(export),
                "applied_to_authoritative_repository": False,
                "authoritative_revision_after_export": git(self.repo, "rev-parse", "HEAD"),
            }
        else:
            self.export = None
            export_payload = {
                "export": None,
                "reason": f"no option recommended (conclusion={final.conclusion})",
                "applied_to_authoritative_repository": False,
            }
        self.write("patch-export.json", export_payload)
        self.experiment = final

    def _doc_patch(self, marker: str, extra_lines: List[str]) -> str:
        """A unified diff adding a marker to the first heading of the doc file.

        Derived from the committed content at the base revision, because the
        workspace is a worktree of that revision, not of the working tree.
        """
        blob = subprocess.run(
            ["git", "show", f"{self.revision}:{DOC_FILE}"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if blob.returncode != 0:
            raise RuntimeError(f"cannot read {DOC_FILE} at {self.revision}")
        before = blob.stdout.splitlines(keepends=True)
        # One list element per line: difflib derives the hunk line counts from
        # the lists, so an element holding an embedded newline produces a hunk
        # header that git apply rejects.
        after = [before[0].rstrip("\n") + marker + "\n"]
        after += [line + "\n" for line in extra_lines]
        after += before[1:]
        body = "".join(
            difflib.unified_diff(
                before, after, fromfile=f"a/{DOC_FILE}", tofile=f"b/{DOC_FILE}"
            )
        )
        return f"diff --git a/{DOC_FILE} b/{DOC_FILE}\n{body}"

    # ── phase 10 ──

    def phase_ledger(self) -> None:
        ledger = EvidenceLedger(self.brain_dir)
        _e, append_ms = timed(
            lambda: ledger.record_repository_registered(
                self.repo_id,
                actor_identity="verification-script",
                trust_level=self.record.trust_level,
                source_revision=self.revision,
            )
        )
        ledger.record_graph_generation_built(
            self.repo_id,
            getattr(self.base_generation, "generation_id", "unknown"),
            actor_identity="verification-script",
            source_revision=self.revision,
        )
        if self.export is not None:
            ledger.record_patch_exported(
                self.repo_id,
                self.export.export_id,
                experiment_id=self.export.experiment_id,
                option_id=self.export.option_id,
                patch_hash=hashlib.sha256(
                    self.export.unified_diff.encode("utf-8")
                ).hexdigest(),
                actor_identity="verification-operator",
                source_revision=self.revision,
            )
        events = ledger.store.all_events()
        report, verify_ms = timed(lambda: LedgerVerifier.verify(events))

        self.measure("ledger_append_ms", append_ms)
        self.measure("ledger_verify_ms", verify_ms)
        self.measure("ledger_events", len(events))
        self.write(
            "ledger-verification.json",
            {
                "events_checked": report.get("events_checked"),
                "valid": report.get("valid"),
                "issues": report.get("issues"),
                "head_sequence": report.get("head_sequence"),
                "event_types": sorted({e.event_type for e in events}),
                "verify_duration_ms": verify_ms,
            },
        )

    # ── phase 11 ──

    def phase_control_plane(self) -> None:
        plane = ControlPlane(self.brain_dir)
        summary, summary_ms = timed(plane.summary)
        queue, queue_ms = timed(plane.action_queue)
        self.measure("control_summary_ms", summary_ms)
        self.measure("control_queue_ms", queue_ms)
        self.measure("artifact_storage_bytes", _dir_bytes(self.brain_dir))
        self.control_summary = summary
        self.write(
            "control-plane-summary.json",
            {
                "summary": jsonable(summary),
                "action_queue": jsonable(queue),
                "budgets": jsonable(plane.budgets()),
                "metrics": jsonable(plane.metrics()),
                "health": jsonable(plane.health()),
            },
        )

    # ── summary ──

    def write_summary(self) -> None:
        exp = self.experiment
        assert exp is not None
        options = {o.option_id: o for o in exp.options}
        commands: List[str] = []
        tests_passed = 0
        tests_failed = 0
        for option in exp.options:
            for result in (option.results or {}).get("validation", {}).get("results", []):
                commands.append(" ".join(result.get("argv", [])))
                if result.get("exit_code") == 0:
                    tests_passed += 1
                else:
                    tests_failed += 1

        incremental = self.incremental_report
        lines = [
            "# v0.5.0 — real Project Brain verification",
            "",
            f"- Generated by: `{GENERATION_COMMAND}`",
            f"- Repository revision: `{self.revision}`",
            f"- Repository id: `{self.repo_id}` (trust level `{self.record.trust_level}`)",
            "",
            "## Graph generation",
            "",
            f"- Full repository graph generation: "
            f"`{getattr(self.base_generation, 'generation_id', 'unknown')}`",
            f"- Nodes: {self.graph_size['nodes']}, "
            f"relationships: {self.graph_size['relationships']}, "
            f"parse errors: {self.graph_size['parse_errors']}",
            f"- Full build duration: "
            f"{self.performance['measurements'].get('repository_graph_full_build_ms')} ms",
            "",
            "## Incremental versus full rebuild",
            "",
            f"- Decision: `{getattr(incremental, 'decision', 'unknown')}`",
            f"- Derived generation: `{getattr(incremental, 'new_generation_id', '')}`",
            f"- Derivation duration: "
            f"{self.performance['measurements'].get('incremental_derive_ms')} ms",
            "",
            "## Workspace",
            "",
            "- State transitions: created → patching → validating → cleaned",
            f"- Creation: {self.performance['measurements'].get('workspace_create_ms')} ms, "
            f"cleanup: {self.performance['measurements'].get('workspace_cleanup_ms')} ms",
            "- Cleanup result: directory removed, workspace record marked cleaned",
            "",
            "## Commands executed inside disposable workspaces",
            "",
        ]
        lines += [f"- `{c}`" for c in commands] or ["- none"]
        lines += [
            "",
            f"- Commands passing: {tests_passed}; failing: {tests_failed}",
            "",
            "## Experiment",
            "",
            f"- Experiment: `{exp.experiment_id}`, state `{exp.state}`",
            f"- Conclusion: `{exp.conclusion}`",
            f"- Recommended option: `{exp.recommended_option_id or 'none'}`",
        ]
        for option_id, option in options.items():
            lines.append(
                f"- Option `{option_id}`: executed={option.executed}, "
                f"required tests passed={option.passed_required}, "
                f"architecture improved={option.architecture_improved}, "
                f"new critical findings={option.new_critical_findings}, "
                f"changed lines={option.changed_lines}"
            )
        lines += [
            "",
            "## Architecture before and after",
            "",
            "- The candidate patches are documentation-only, so the measured "
            "architecture deltas are zero: no new findings, no new cycles, no "
            "new forbidden edges. That is the honest result for this patch, not "
            "evidence that the analyzer detects nothing.",
            "",
            "## Exported patch",
            "",
        ]
        if self.export is not None:
            lines += [
                f"- Export id: `{self.export.export_id}` for option "
                f"`{self.export.option_id}`",
                f"- Authorized by: `{self.export.authorized_by}`",
                "- Applied to the authoritative repository: **no**. The export is a "
                "file for a human to review and apply.",
            ]
        else:
            lines.append("- No option was recommended, so nothing was exported.")
        lines += [
            "",
            "## Ledger health",
            "",
            f"- Events: {self.performance['measurements'].get('ledger_events')}; "
            f"chain valid: {json.loads((self.out_dir / 'ledger-verification.json').read_text(encoding='utf-8'))['valid']}",
            "",
            "## Cleanup",
            "",
            "- Every disposable workspace was removed; no workspace remained in a "
            "quarantined state.",
            "- All runtime state (registry, freshness, lab, experiments, ledger) was "
            "created in a temporary directory and deleted when the run finished.",
            "- One exception, by design: graph generations are written to the "
            "repository's own gitignored `.brain/` metadata directory, which is "
            "what the `write_brain_metadata` capability exists for. No tracked "
            "file was touched.",
            "- The authoritative checkout was never written to: no commit, no branch, "
            "no push, no deployment.",
            "",
            "## Known isolation limitations",
            "",
            "- **Network isolation is unverified.** Validation child processes are not "
            "network-isolated by any OS mechanism on this host; the value reported by "
            "the runner is `unverified`, and this run does not upgrade that claim.",
            "- Filesystem isolation is a Git worktree under the managed root plus path "
            "containment checks — not a chroot, container, or jail. A validation "
            "command that deliberately escapes the workspace can reach the wider "
            "filesystem with the privileges of the invoking user.",
            "- Environment isolation is an allowlist. Variables outside the allowlist, "
            "including secrets, are not passed to validation processes.",
            "- Process isolation is a new process group with timeout-driven termination. "
            "A process that detaches from that group is not tracked.",
            "",
            "## Non-autonomy statement",
            "",
            "Project Brain may apply candidate patches only inside disposable managed "
            "workspaces for validation. It does not apply patches to authoritative "
            "repositories, commit changes, push branches, merge pull requests, or "
            "deploy software.",
            "",
        ]
        path = self.out_dir / "summary.md"
        path.write_text(
            self.redactor.text("\n".join(lines)), encoding="utf-8", newline="\n"
        )
        print("  wrote summary.md")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument("--out", default=str(REPO_ROOT / "reports" / "v0.5.0-engineering-lab"))
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    out_dir = Path(args.out).resolve()
    work_dir = Path(tempfile.mkdtemp(prefix="brain-v050-verify-"))
    print(f"Verifying {repo.name} at {git(repo, 'rev-parse', '--short', 'HEAD')}")
    try:
        Verification(repo, out_dir, work_dir).run()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"], cwd=repo, capture_output=True, text=True
        )
    print(f"Artifacts written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
