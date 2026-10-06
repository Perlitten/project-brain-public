import hashlib
import json
import shutil
from pathlib import Path

import pytest

from scripts.validate_lfm_release_gate import (
    GateError,
    load_env_file,
    release_binding_digest,
    validate_release_gate,
)
from scripts.write_source_manifest import build_manifest, validate_release_identity


ROOT = Path(__file__).resolve().parents[1]


def test_production_image_is_immutable_and_ui_is_baked_into_it():
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert compose.count("image: brain-api:${BRAIN_IMAGE_TAG:-latest}") == 2
    assert "./apps/api/templates:/app/apps/api/templates" not in compose
    assert "./apps/api/static:/app/apps/api/static" not in compose
    assert "./eval:/app/eval" not in compose
    assert "COPY eval ./eval" in dockerfile
    assert "!eval/" in dockerignore
    assert "!eval/**" in dockerignore
    assert "**/__pycache__/**" in dockerignore
    assert "**/*.py[cod]" in dockerignore
    assert "\n      BRAIN_BUILD_SHA:" not in compose
    assert "\n      BRAIN_BUILD_TIME:" not in compose
    assert "\n      BRAIN_SOURCE_DIGEST:" not in compose


def test_release_script_refuses_dirty_git_and_uses_sha_image_tag():
    script = (ROOT / "deploy" / "server_up.sh").read_text(encoding="utf-8")
    assert "git status --porcelain --untracked-files=normal" in script
    assert 'BRAIN_IMAGE_TAG="$BRAIN_BUILD_SHA"' in script
    assert 'set_env_value BRAIN_IMAGE_TAG "$BRAIN_IMAGE_TAG"' in script
    assert (
        "export BRAIN_BUILD_SHA BRAIN_BUILD_TIME BRAIN_SOURCE_DIGEST "
        "BRAIN_DEPLOY_DIGEST BRAIN_IMAGE_TAG"
    ) in script
    assert "--include eval" in script
    assert "--include deploy/server_up.sh" in script


def test_production_release_script_refuses_missing_or_red_github_ci_before_building():
    script = (ROOT / "deploy" / "server_up.sh").read_text(encoding="utf-8")
    production_example = (ROOT / "deploy" / ".env.prod.example").read_text(encoding="utf-8")
    deploy_example = (ROOT / ".deploy.env.example").read_text(encoding="utf-8")

    assert "verify_production_ci() {" in script
    assert "/actions/workflows/${workflow}/runs?head_sha=${BRAIN_BUILD_SHA}" in script
    assert 'Authorization: Bearer ${github_token}' in script
    assert "requires BRAIN_GITHUB_TOKEN with Actions read access" in script
    assert 'latest.get("conclusion") != "success"' in script
    assert "verify_production_ci || exit 1" in script
    assert script.index("verify_production_ci || exit 1") < script.index("==> Backing up PostgreSQL")
    assert script.index("verify_production_ci || exit 1") < script.index("$COMPOSE build")
    assert 'DEPLOY_ENV_FILE=".deploy.env"' in script
    assert "github_token=\"$(deploy_env_value BRAIN_GITHUB_TOKEN)\"" in script
    assert "BRAIN_GITHUB_REPOSITORY=Perlitten/project-brain-public" in production_example
    assert "BRAIN_GITHUB_CI_WORKFLOW=ci.yml" in production_example
    assert "BRAIN_GITHUB_TOKEN=" not in production_example
    assert "BRAIN_GITHUB_TOKEN=" in deploy_example


def test_github_ci_token_is_excluded_from_compose_runtime_env_file_contract():
    script = (ROOT / "deploy" / "server_up.sh").read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert ".deploy.env" in gitignore
    assert "deploy_env_value()" in script
    assert "github_token=\"$(env_value BRAIN_GITHUB_TOKEN)\"" not in script


def test_production_compose_requires_verified_baked_release_identity():
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert compose.count("${BRAIN_BUILD_SHA:?BRAIN_BUILD_SHA must be set for production}") == 2
    assert compose.count(
        "${BRAIN_SOURCE_DIGEST:?BRAIN_SOURCE_DIGEST must be set for production}"
    ) == 2
    assert compose.count(
        "${BRAIN_DEPLOY_DIGEST:?BRAIN_DEPLOY_DIGEST must be set for production}"
    ) == 2
    assert compose.count('BRAIN_REQUIRE_RELEASE_IDENTITY: "true"') == 2
    assert "ARG BRAIN_REQUIRE_RELEASE_IDENTITY=false" in dockerfile
    assert '--expected-content-digest "${BRAIN_SOURCE_DIGEST}"' in dockerfile
    assert '--expected-content-digest "${BRAIN_DEPLOY_DIGEST}"' in dockerfile
    assert "--require-release-identity" in dockerfile


def test_release_script_enforces_late_interaction_production_gate():
    script = (ROOT / "deploy" / "server_up.sh").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    production_example = (ROOT / "deploy" / ".env.prod.example").read_text(
        encoding="utf-8"
    )

    assert "LATE_INTERACTION_EXPERIMENT_AUTHORIZED=false" in example
    assert "LATE_INTERACTION_PRODUCTION_GATES_PASSED=false" in example
    assert "LATE_INTERACTION_EXPERIMENT_AUTHORIZED=false" in production_example
    assert "LATE_INTERACTION_PRODUCTION_GATES_PASSED=false" in production_example
    assert "scripts/validate_lfm_release_gate.py" in script
    assert '--build-sha "$BRAIN_BUILD_SHA"' in script
    assert '--source-digest "$BRAIN_SOURCE_DIGEST"' in script
    assert '--reports-root "$(pwd)/reports"' in script
    assert '$COMPOSE config --format json > "$compose_config_temp"' in script
    assert '--effective-compose-config "$compose_config_temp"' in script
    assert "trap cleanup_preflight_files EXIT" in script
    assert "LATE_INTERACTION_REMOTE_EXPECTED_INDEX_REVISION=" in production_example
    assert "LATE_INTERACTION_APPROVED_REPOSITORY_ID=" in production_example
    assert "LATE_INTERACTION_APPROVED_SOURCE_DIGEST=" in production_example
    assert "LATE_INTERACTION_APPROVED_LINEAGE_ID=" in production_example
    assert "LATE_INTERACTION_APPROVED_IDENTITY_DIGEST=" in production_example
    assert "LATE_INTERACTION_APPROVED_DOCUMENT_COUNT=" in production_example
    assert "LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH=" in production_example


def test_release_script_fails_closed_on_invalid_boolean_values():
    script = (ROOT / "deploy" / "server_up.sh").read_text(encoding="utf-8")

    assert (
        'self_diagnosis_enabled="$(env_bool SELF_DIAGNOSIS_ENABLED)" || exit 1'
        in script
    )
    assert (
        'telegram_alerts_enabled="$(env_bool TELEGRAM_ALERTS_ENABLED)" || exit 1'
        in script
    )
    assert 'if [[ "$self_diagnosis_enabled" == "true" ]]' in script
    assert 'if [[ "$telegram_alerts_enabled" != "true" ]]' in script


def _experiment_env(truthy: str = "true") -> dict[str, str]:
    return {
        "LATE_INTERACTION_ENABLED": truthy,
        "LATE_INTERACTION_EXPERIMENT_AUTHORIZED": truthy,
        "LATE_INTERACTION_REMOTE_ENABLED": truthy,
        "LATE_INTERACTION_REMOTE_MODEL_REVISION": "model-r1",
        "LATE_INTERACTION_REMOTE_EXPECTED_INDEX_REVISION": "r1",
        "LATE_INTERACTION_APPROVED_REPOSITORY_ID": "7",
        "LATE_INTERACTION_APPROVED_BUILD_SHA": "build-r1",
        "LATE_INTERACTION_APPROVED_SOURCE_DIGEST": "d" * 64,
        "LATE_INTERACTION_APPROVED_MODEL_REVISION": "model-r1",
        "LATE_INTERACTION_APPROVED_INDEX_REVISION": "r1",
        "LATE_INTERACTION_APPROVED_LINEAGE_ID": "lineage-r1",
        "LATE_INTERACTION_APPROVED_IDENTITY_DIGEST": "e" * 64,
        "LATE_INTERACTION_APPROVED_DOCUMENT_COUNT": "1",
    }


def _release_binding() -> dict[str, object]:
    return {
        "repository_id": 7,
        "build_sha": "build-r1",
        "source_digest": "d" * 64,
        "model_revision": "model-r1",
        "index_revision": "r1",
        "lineage_id": "lineage-r1",
        "identity_digest": "e" * 64,
        "document_count": 1,
    }


def _write_release_approval(tmp_path: Path) -> tuple[Path, str, dict]:
    evidence_dir = tmp_path / "lfm"
    evidence_dir.mkdir(exist_ok=True)
    binding = _release_binding()
    artifact_payloads = {
        "quality": {
            "schema_version": 2,
            "release_binding": binding,
            "overall_gate": {"passed": True},
            "evaluation_provenance": {
                "policy": "lfm-production-v1",
                "production_gate": True,
                "fixed_thresholds": True,
                "thresholds": {
                    "median_ndcg_delta_min": 0.03,
                    "improved_ratio_min": 0.60,
                    "hit3_regression_max": 0.10,
                    "slice_regression_max": 0.05,
                },
            },
        },
        "runtime_latency_slo": {
            "schema": "project-brain.lfm-runtime-latency",
            "schema_version": 1,
            "passed": True,
            "release_binding": binding,
            "summary": {
                "p95_end_to_end_increase_ms": 400.0,
                "provider_failure_rate": 0.0005,
                "fail_open_verified": True,
                "sample_count": 500,
            },
        },
        "runtime_parity": {
            "schema_version": 1,
            "release_binding": binding,
            "gate": {
                "passed": True,
                "thresholds": {
                    "overlap_min": 0.90,
                    "spearman_min": 0.95,
                    "kendall_min": 0.90,
                    "ndcg_abs_delta_max": 0.02,
                },
            },
            "summary": {
                "count": 10,
                "mean_top_k_overlap": 0.95,
                "mean_spearman": 0.97,
                "mean_kendall": 0.93,
                "max_abs_ndcg_delta": 0.01,
            },
        },
        "production_shadow": {
            "schema": "project-brain.lfm-production-shadow",
            "schema_version": 1,
            "passed": True,
            "release_binding": binding,
            "summary": {
                "production_real": True,
                "window_days": 7.0,
                "successfully_recorded_eligible_queries": 500,
                "scored_count": 500,
                "skipped_count": 0,
                "error_count": 0,
            },
        },
    }
    binding_hash = release_binding_digest(binding)
    sections: dict[str, dict] = {}
    for name, artifact in artifact_payloads.items():
        artifact_path = evidence_dir / f"{name}.json"
        artifact_path.write_text(
            json.dumps(artifact, sort_keys=True),
            encoding="utf-8",
        )
        sections[name] = {
            "schema_version": 1,
            "passed": True,
            "binding_sha256": binding_hash,
            "evidence_path": f"lfm/{name}.json",
            "evidence_sha256": hashlib.sha256(
                artifact_path.read_bytes()
            ).hexdigest(),
        }
    sections["quality"].update(
        {
            "report_schema_version": 2,
            "overall_gate_passed": True,
            "production_gate": True,
            "fixed_thresholds": True,
            "policy": "lfm-production-v1",
            "thresholds": {
                "median_ndcg_delta_min": 0.03,
                "improved_ratio_min": 0.60,
                "hit3_regression_max": 0.10,
                "slice_regression_max": 0.05,
            },
        }
    )
    sections["runtime_latency_slo"].update(
        artifact_payloads["runtime_latency_slo"]["summary"]
    )
    sections["runtime_parity"].update(
        {
            "sample_count": 10,
            **artifact_payloads["runtime_parity"]["summary"],
        }
    )
    sections["runtime_parity"].pop("count")
    sections["production_shadow"].update(
        artifact_payloads["production_shadow"]["summary"]
    )
    approval = {
        "schema": "project-brain.lfm-release-approval",
        "schema_version": 1,
        "passed": True,
        "release_binding": binding,
        "gates": sections,
    }
    approval_path = evidence_dir / "approval.json"
    approval_path.write_text(
        json.dumps(approval, sort_keys=True),
        encoding="utf-8",
    )
    return (
        approval_path,
        hashlib.sha256(approval_path.read_bytes()).hexdigest(),
        approval,
    )


@pytest.mark.parametrize("truthy", ["true", "TRUE", "1", "yes", "on"])
def test_lfm_release_preflight_accepts_all_documented_truthy_values(
    tmp_path,
    truthy,
):
    validate_release_gate(
        _experiment_env(truthy),
        build_sha="build-r1",
        source_digest="d" * 64,
        reports_root=tmp_path,
    )


def test_lfm_release_preflight_rejects_duplicate_critical_keys(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "LATE_INTERACTION_ENABLED=false\nLATE_INTERACTION_ENABLED=true\n",
        encoding="utf-8",
    )
    with pytest.raises(GateError, match="duplicate"):
        load_env_file(env_path)


def test_lfm_release_preflight_verifies_real_evidence_file(tmp_path):
    evidence_path, evidence_hash, _ = _write_release_approval(tmp_path)
    values = _experiment_env() | {
        "LATE_INTERACTION_RERANK_ENABLED": "true",
        "LATE_INTERACTION_PRODUCTION_GATES_PASSED": "true",
        "LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH": str(
            evidence_path.relative_to(tmp_path)
        ),
        "LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256": evidence_hash,
    }
    validate_release_gate(
        values,
        build_sha="build-r1",
        source_digest="d" * 64,
        reports_root=tmp_path,
    )
    with pytest.raises(GateError, match="does not match"):
        validate_release_gate(
            values
            | {"LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256": "f" * 64},
            build_sha="build-r1",
            source_digest="d" * 64,
            reports_root=tmp_path,
        )


def test_lfm_release_preflight_rejects_quality_only_report(tmp_path):
    quality_path = tmp_path / "paired-eval.json"
    quality_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "overall_gate": {"passed": True},
                "evaluation_provenance": {
                    "policy": "lfm-production-v1",
                    "production_gate": True,
                    "fixed_thresholds": True,
                },
                "release_binding": _release_binding(),
            }
        ),
        encoding="utf-8",
    )
    values = _experiment_env() | {
        "LATE_INTERACTION_RERANK_ENABLED": "true",
        "LATE_INTERACTION_PRODUCTION_GATES_PASSED": "true",
        "LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH": quality_path.name,
        "LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256": hashlib.sha256(
            quality_path.read_bytes()
        ).hexdigest(),
    }
    with pytest.raises(GateError, match="release approval schema"):
        validate_release_gate(
            values,
            build_sha="build-r1",
            source_digest="d" * 64,
            reports_root=tmp_path,
        )


@pytest.mark.parametrize("payload", [b"", b"{not-json", b"[]"])
def test_lfm_release_preflight_rejects_empty_or_malformed_evidence(
    tmp_path,
    payload,
):
    evidence_path = tmp_path / "approval.json"
    evidence_path.write_bytes(payload)
    values = _experiment_env() | {
        "LATE_INTERACTION_RERANK_ENABLED": "true",
        "LATE_INTERACTION_PRODUCTION_GATES_PASSED": "true",
        "LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH": evidence_path.name,
        "LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256": hashlib.sha256(
            payload
        ).hexdigest(),
    }
    with pytest.raises(GateError):
        validate_release_gate(
            values,
            build_sha="build-r1",
            source_digest="d" * 64,
            reports_root=tmp_path,
        )


def test_lfm_release_preflight_rejects_oversized_evidence(tmp_path):
    evidence_path = tmp_path / "approval.json"
    evidence_path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    values = _experiment_env() | {
        "LATE_INTERACTION_RERANK_ENABLED": "true",
        "LATE_INTERACTION_PRODUCTION_GATES_PASSED": "true",
        "LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH": evidence_path.name,
        "LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256": hashlib.sha256(
            evidence_path.read_bytes()
        ).hexdigest(),
    }
    with pytest.raises(GateError, match="between 1"):
        validate_release_gate(
            values,
            build_sha="build-r1",
            source_digest="d" * 64,
            reports_root=tmp_path,
        )


def test_lfm_release_preflight_rejects_missing_or_tampered_gate_artifact(tmp_path):
    evidence_path, evidence_hash, approval = _write_release_approval(tmp_path)
    values = _experiment_env() | {
        "LATE_INTERACTION_RERANK_ENABLED": "true",
        "LATE_INTERACTION_PRODUCTION_GATES_PASSED": "true",
        "LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH": str(
            evidence_path.relative_to(tmp_path)
        ),
        "LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256": evidence_hash,
    }
    parity_path = tmp_path / approval["gates"]["runtime_parity"]["evidence_path"]
    parity_path.write_text('{"tampered":true}', encoding="utf-8")
    with pytest.raises(GateError, match="SHA-256"):
        validate_release_gate(
            values,
            build_sha="build-r1",
            source_digest="d" * 64,
            reports_root=tmp_path,
        )
    parity_path.unlink()
    with pytest.raises(GateError, match="missing"):
        validate_release_gate(
            values,
            build_sha="build-r1",
            source_digest="d" * 64,
            reports_root=tmp_path,
        )


def test_lfm_release_preflight_rejects_effective_compose_override(tmp_path):
    values = _experiment_env()
    environment = dict(values)
    compose_path = tmp_path / "compose.json"
    compose_path.write_text(
        json.dumps(
            {
                "services": {
                    "api": {"environment": environment},
                    "worker": {
                        "environment": environment
                        | {"LATE_INTERACTION_ENABLED": "false"}
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(GateError, match="Compose override diverges"):
        validate_release_gate(
            values,
            build_sha="build-r1",
            source_digest="d" * 64,
            reports_root=tmp_path,
            effective_compose_path=compose_path,
        )


def test_image_manifest_digest_matches_canonical_build_input_digest(tmp_path):
    includes = [
        "pyproject.toml",
        "README.md",
        "brain",
        "apps",
        "rules",
        "eval",
        "scripts/write_source_manifest.py",
        "scripts/validate_lfm_release_gate.py",
    ]
    copied_root = tmp_path / "image"
    copied_root.mkdir()
    for value in includes:
        source = ROOT / value
        destination = copied_root / value
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)

    host_manifest = build_manifest(
        ROOT,
        "build-r1",
        includes=includes,
    )
    image_manifest = build_manifest(copied_root, "build-r1")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert host_manifest["content_digest"] == image_manifest["content_digest"]
    assert dockerfile.index("write_source_manifest.py /build") < dockerfile.index(
        "pip install --no-cache-dir"
    )


def test_deployment_control_files_have_a_separate_verified_digest(tmp_path):
    includes = [
        "Dockerfile",
        "docker-compose.prod.yml",
        "deploy/server_up.sh",
        "scripts/write_source_manifest.py",
        "scripts/validate_lfm_release_gate.py",
    ]
    copied_root = tmp_path / "release-inputs"
    copied_root.mkdir()
    for value in includes:
        source = ROOT / value
        destination = copied_root / value
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    host_manifest = build_manifest(ROOT, "build-r1", includes=includes)
    image_manifest = build_manifest(copied_root, "build-r1")
    assert host_manifest["content_digest"] == image_manifest["content_digest"]


def test_manifest_release_identity_rejects_unknown_or_mismatched_production_build(
    tmp_path,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload.txt").write_text("immutable\n", encoding="utf-8")
    manifest = build_manifest(source, "build-r1")

    validate_release_identity(
        manifest,
        expected_content_digest=manifest["content_digest"],
        require_release_identity=True,
    )
    with pytest.raises(ValueError, match="does not match"):
        validate_release_identity(
            manifest,
            expected_content_digest="f" * 64,
            require_release_identity=True,
        )
    with pytest.raises(ValueError, match="must not be unknown"):
        validate_release_identity(
            build_manifest(source, "unknown"),
            expected_content_digest=manifest["content_digest"],
            require_release_identity=True,
        )
    with pytest.raises(ValueError, match="64-character"):
        validate_release_identity(
            manifest,
            expected_content_digest="unknown",
            require_release_identity=True,
        )


def test_lfm_release_preflight_rejects_symlinked_evidence(tmp_path):
    target = tmp_path / "real.json"
    target.write_text('{"approved":true}\n', encoding="utf-8")
    link = tmp_path / "approval.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    values = _experiment_env() | {
        "LATE_INTERACTION_RERANK_ENABLED": "true",
        "LATE_INTERACTION_PRODUCTION_GATES_PASSED": "true",
        "LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH": link.name,
        "LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256": hashlib.sha256(
            target.read_bytes()
        ).hexdigest(),
    }
    with pytest.raises(GateError, match="symlink"):
        validate_release_gate(
            values,
            build_sha="build-r1",
            source_digest="d" * 64,
            reports_root=tmp_path,
        )


def test_lfm_release_preflight_rejects_parent_path_escape(tmp_path):
    outside = tmp_path.parent / "outside-approval.json"
    outside.write_text('{"approved":true}\n', encoding="utf-8")
    values = _experiment_env() | {
        "LATE_INTERACTION_RERANK_ENABLED": "true",
        "LATE_INTERACTION_PRODUCTION_GATES_PASSED": "true",
        "LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH": "../outside-approval.json",
        "LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256": hashlib.sha256(
            outside.read_bytes()
        ).hexdigest(),
    }
    with pytest.raises(GateError, match="escapes"):
        validate_release_gate(
            values,
            build_sha="build-r1",
            source_digest="d" * 64,
            reports_root=tmp_path,
        )


def test_worker_and_deploy_doctor_use_closed_loop_readiness():
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    doctor = (ROOT / "deploy" / "doctor.sh").read_text(encoding="utf-8")
    assert "WORKER_REDIS_PREFIX" in compose
    assert ":heartbeat" in compose
    assert "/ready" in doctor
    assert 'api_port}/health"' not in doctor


def test_schema_initialization_is_serialized_across_api_and_worker():
    session_source = (ROOT / "brain" / "database" / "session.py").read_text(encoding="utf-8")
    migrations_source = (ROOT / "brain" / "database" / "migrations.py").read_text(encoding="utf-8")
    assert "pg_advisory_lock" in session_source or "pg_try_advisory_lock" in session_source
    assert "pg_advisory_unlock" in session_source or "pg_try_advisory_lock" in session_source
    assert "await _init_db_unlocked()" in session_source
    assert "apply_migrations(conn, acquire_lock=False)" in session_source
    assert "pg_advisory_xact_lock" in migrations_source


def test_release_image_retention_runs_after_health_and_preserves_the_rollback_window():
    """Old brain-api:<sha> images are the rollback mechanism, and they also filled
    this host's disk to 95%. Retention is therefore allowed to delete them only
    after the deploy proved healthy, only within the brain-api repository, and
    never so aggressively that the running or a referenced image disappears."""
    script = (ROOT / "deploy" / "server_up.sh").read_text(encoding="utf-8")
    production_example = (ROOT / "deploy" / ".env.prod.example").read_text(encoding="utf-8")

    assert "prune_old_release_images() {" in script
    assert "BRAIN_IMAGE_RETENTION=5" in production_example

    prune_at = script.index("prune_old_release_images \\\n")
    assert prune_at > script.index("==> HEALTH OK")
    assert prune_at > script.index("==> Scheduler state")
    # Fail-safe: the call sits in an `||` context so `set -e` cannot turn a
    # cleanup error into a failed deploy.
    assert "prune_old_release_images \\\n    || echo" in script

    assert "'$2 == \"brain-api\" && $3 != \"<none>\" { print $1 \"|\" $3 }'" in script
    assert 'docker rmi "brain-api:${tag}"' in script
    assert '[[ "$tag" == "$BRAIN_IMAGE_TAG" ]] && continue' in script
    assert "docker ps -a --format '{{.Image}}'" in script
    assert "(( index <= keep )) && continue" in script


def test_deploy_script_takes_an_exclusive_lock_before_mutating_state():
    """A concurrent deployment already corrupted this host once: two runs raced,
    one overwrote the release stamp the other had written, and production served
    a tree that existed in no branch. The mutex must therefore come before .env,
    the release stamp, the backups and any container mutation — after any of
    those, a second run has already done damage."""
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "deploy" / "server_up.sh").read_text(
        encoding="utf-8"
    )
    assert "flock" in script, "deploy/server_up.sh must take an exclusive lock"

    lock_at = script.index("flock -n 9")
    for state_change, what in (
        ("ENV_FILE=", "reads or writes .env"),
        (".project-brain-release", "stamps the release identity"),
        ("Backing up", "backs up a datastore"),
        ("$COMPOSE up", "mutates containers"),
    ):
        if state_change in script:
            assert script.index(state_change) > lock_at, (
                f"deploy/server_up.sh {what} before acquiring the lock; a second "
                "run can corrupt it in the window before the mutex is taken"
            )
