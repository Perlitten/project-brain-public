"""Focused tests for Workstream F — the engineering evidence ledger.

Sections mirror the specification phases: F2 event model and canonical
hashing, F4 sensitive-data handling, F3 append-only storage and deterministic
export, F5 verification and CLI, F6 read-only API, F1 product-action coverage.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from brain.ledger.cli import main as ledger_cli
from brain.ledger.ledger import EvidenceLedger
from brain.ledger.models import (
    GENESIS_HASH,
    SCHEMA_VERSION,
    ActorType,
    EventType,
    LedgerEvent,
    LedgerIntegrityError,
    new_event_id,
    redact_text,
    sanitize_metadata,
)
from brain.ledger.store import MAX_PAGE_SIZE, LedgerStore, build_event
from brain.ledger.verifier import (
    ALTERED_EVENT,
    BROKEN_PREVIOUS_HASH,
    DUPLICATE_SEQUENCE,
    MISSING_EVENT,
    REORDERED_EVENT,
    UNSUPPORTED_SCHEMA,
    LedgerVerifier,
)


@pytest.fixture
def ledger(tmp_path) -> EvidenceLedger:
    return EvidenceLedger(tmp_path / ".brain")


@pytest.fixture
def store(tmp_path) -> LedgerStore:
    return LedgerStore(tmp_path / "ledger.sqlite3")


def _event(**kwargs) -> LedgerEvent:
    params = {
        "event_type": EventType.FINDING_CREATED.value,
        "actor_identity": "project-brain",
        "repository_id": "repo-1",
        "entity_type": "finding",
        "entity_id": "find-1",
        "action": "create",
    }
    params.update(kwargs)
    return build_event(params.pop("event_type"), **params)


def _seed(store: LedgerStore, count: int = 5) -> list[LedgerEvent]:
    return [store.append(_event(entity_id=f"find-{i}")) for i in range(count)]


# ── F2: event model and canonical hashing ──


def test_event_carries_every_mandated_field():
    fields = set(LedgerEvent.__dataclass_fields__)
    required = {
        "event_id",
        "schema_version",
        "event_type",
        "timestamp_utc",
        "actor_identity",
        "actor_type",
        "repository_id",
        "portfolio_id",
        "entity_type",
        "entity_id",
        "action",
        "previous_state",
        "new_state",
        "reason",
        "source_revision",
        "artifact_references",
        "metadata",
        "previous_hash",
        "event_hash",
    }
    assert required <= fields


def test_canonical_serialization_is_deterministic_and_order_independent():
    a = _event()
    b = LedgerEvent.from_dict(dict(reversed(list(a.to_dict().items()))))
    assert a.canonical_payload() == b.canonical_payload()
    assert a.compute_hash() == b.compute_hash()
    # Sorted keys, no insignificant whitespace.
    payload = json.loads(a.canonical_payload())
    assert list(payload) == sorted(payload)
    assert ", " not in a.canonical_payload()


def test_hash_excludes_only_itself():
    event = _event().seal()
    assert LedgerEvent.HASH_EXCLUDED == ("event_hash",)
    assert "event_hash" not in json.loads(event.canonical_payload())
    assert "previous_hash" in json.loads(event.canonical_payload())
    assert "sequence" in json.loads(event.canonical_payload())


@pytest.mark.parametrize(
    "field_name, value",
    [
        ("reason", "changed reason"),
        ("new_state", "different"),
        ("source_revision", "deadbeef"),
        ("sequence", 99),
        ("previous_hash", "f" * 64),
        ("timestamp_utc", "2000-01-01T00:00:00Z"),
    ],
)
def test_any_field_change_changes_the_hash(field_name, value):
    original = _event().seal()
    mutated = replace(original, **{field_name: value})
    assert mutated.compute_hash() != original.event_hash


def test_identity_fields_are_normalized_before_hashing():
    a = _event(actor_identity="  Alice ", entity_type="Finding", action=" Create ")
    b = _event(actor_identity="alice", entity_type="finding", action="create")
    assert a.actor_identity == "alice"
    assert a.entity_type == "finding"
    assert a.action == "create"
    # Same identity, same canonical form: one entity, not two histories.
    assert a.entity_type == b.entity_type and a.actor_identity == b.actor_identity


def test_artifact_references_are_deduplicated_and_ordered():
    event = _event(artifact_references=["b.json", "a.json", "b.json", ""])
    assert event.artifact_references == ["a.json", "b.json"]


def test_new_event_ids_are_unique():
    assert len({new_event_id() for _ in range(200)}) == 200


# ── F4: sensitive-data handling ──


@pytest.mark.parametrize(
    "key",
    ["API_KEY", "DATABASE_URL", "password", "auth_token", "PRIVATE_KEY", "session_cookie"],
)
def test_sensitive_keys_are_stored_as_digests(key):
    safe, redacted = sanitize_metadata({key: "super-secret-value"})
    assert safe[key].startswith("sha256:")
    assert "super-secret-value" not in json.dumps(safe)
    assert key in redacted


@pytest.mark.parametrize("key", ["environment", "env", "headers", "stdout", "stderr", "raw_log"])
def test_whole_payload_keys_are_never_embedded(key):
    safe, redacted = sanitize_metadata({key: {"PATH": "/usr/bin", "SECRET": "x"}})
    assert safe[key] == "<REDACTED>"
    assert key in redacted


def test_credential_shaped_free_text_is_stripped():
    text = "connect via postgres://user:hunter2@db:5432/brain with api_key=abc123"
    cleaned = redact_text(text)
    assert "hunter2" not in cleaned
    assert "abc123" not in cleaned
    assert "postgres://" in cleaned


def test_nested_metadata_is_sanitized_and_recorded(ledger):
    event = ledger.record_finding_created(
        "repo-1",
        "find-secret",
        severity="critical",
        metadata={"context": {"db": {"DATABASE_URL": "postgres://u:p@h/db"}}},
    )
    assert "postgres://u:p@h/db" not in json.dumps(event.to_dict())
    assert event.redacted_fields == ["context.db.DATABASE_URL"]


def test_patch_content_is_recorded_by_digest_only(ledger):
    event = ledger.record_patch_applied_in_workspace(
        "repo-1", "ws-1", patch_id="patch-1", patch_hash="a" * 64, changed_files=2
    )
    dumped = json.dumps(event.to_dict())
    assert "a" * 64 in dumped
    assert "diff --git" not in dumped
    assert event.metadata["scope"] == "disposable_managed_workspace"


# ── F3: append-only storage ──


def test_first_event_chains_from_genesis(store):
    event = store.append(_event())
    assert event.sequence == 1
    assert event.previous_hash == GENESIS_HASH
    assert event.event_hash == event.compute_hash()


def test_sequences_are_dense_and_hashes_chain(store):
    events = _seed(store, 6)
    assert [e.sequence for e in events] == [1, 2, 3, 4, 5, 6]
    for previous, current in zip(events, events[1:]):
        assert current.previous_hash == previous.event_hash


def test_duplicate_event_id_is_rejected_transactionally(store):
    first = store.append(_event())
    clash = _event()
    clash.event_id = first.event_id
    with pytest.raises(LedgerIntegrityError):
        store.append(clash)
    # The failed append must leave no partial row and no consumed sequence.
    assert store.count() == 1
    assert store.append(_event()).sequence == 2


def test_update_is_blocked_by_the_storage_engine(store):
    _seed(store, 2)
    conn = sqlite3.connect(str(store.db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE ledger_events SET reason = 'x' WHERE sequence = 1")
    finally:
        conn.close()


def test_delete_is_blocked_by_the_storage_engine(store):
    _seed(store, 2)
    conn = sqlite3.connect(str(store.db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM ledger_events WHERE sequence = 1")
    finally:
        conn.close()


def test_unsupported_schema_version_cannot_be_written(store):
    event = _event()
    event.schema_version = SCHEMA_VERSION + 7
    with pytest.raises(LedgerIntegrityError):
        store.append(event)
    assert store.count() == 0


def test_query_is_bounded_and_paginated(store):
    _seed(store, 12)
    page = store.query(limit=5, offset=0)
    assert page["returned"] == 5 and page["total"] == 12 and page["has_more"] is True
    second = store.query(limit=5, offset=5)
    assert [e["sequence"] for e in second["events"]] == [6, 7, 8, 9, 10]
    last = store.query(limit=5, offset=10)
    assert last["returned"] == 2 and last["has_more"] is False


def test_query_limit_is_clamped_to_the_maximum(store):
    _seed(store, 3)
    assert store.query(limit=10_000)["limit"] == MAX_PAGE_SIZE
    assert store.query(limit=0)["limit"] > 0


def test_query_filters_by_type_repository_and_actor(store):
    store.append(_event(repository_id="repo-a", actor_identity="alice"))
    store.append(_event(repository_id="repo-b", actor_identity="bob"))
    store.append(
        _event(
            event_type=EventType.WORKSPACE_CREATED.value,
            repository_id="repo-a",
            entity_type="workspace",
        )
    )
    assert store.query(repository_id="repo-a")["total"] == 2
    assert store.query(actor_identity="bob")["total"] == 1
    assert store.query(event_type=EventType.WORKSPACE_CREATED.value)["total"] == 1


def test_since_sequence_supports_incremental_reads(store):
    _seed(store, 5)
    tail = store.query(since_sequence=3)
    assert [e["sequence"] for e in tail["events"]] == [4, 5]


def test_entity_history_returns_only_that_entity(store):
    store.append(_event(entity_id="find-x"))
    store.append(_event(entity_id="find-y"))
    store.append(_event(entity_id="find-x", action="resolve"))
    history = store.entity_history("finding", "find-x")
    assert history["total"] == 2
    assert {e["action"] for e in history["events"]} == {"create", "resolve"}


def test_get_returns_none_for_unknown_event(store):
    assert store.get("evt-nope") is None


# ── F3: deterministic export ──


def test_export_is_byte_identical_across_runs(store, tmp_path):
    _seed(store, 4)
    first = store.export_jsonl(tmp_path / "a.jsonl")
    second = store.export_jsonl(tmp_path / "b.jsonl")
    assert first["sha256"] == second["sha256"]
    assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes()


def test_export_round_trips_through_verification(store, tmp_path):
    _seed(store, 4)
    path = tmp_path / "ledger.jsonl"
    store.export_jsonl(path)
    reloaded = LedgerStore.load_jsonl(path)
    assert [e.sequence for e in reloaded] == [1, 2, 3, 4]
    assert LedgerVerifier.verify(reloaded)["valid"] is True


def test_export_of_empty_ledger_is_valid(store, tmp_path):
    result = store.export_jsonl(tmp_path / "empty.jsonl")
    assert result["events"] == 0
    assert result["head_hash"] == GENESIS_HASH
    assert LedgerVerifier.verify(LedgerStore.load_jsonl(tmp_path / "empty.jsonl"))["valid"]


# ── F5: verification failure modes ──


def test_clean_chain_verifies(store):
    _seed(store, 5)
    report = LedgerVerifier.verify(store.all_events())
    assert report["valid"] is True
    assert report["issues"] == []
    assert report["head_sequence"] == 5


def test_verifier_detects_altered_event_content(store):
    events = _seed(store, 4)
    events[2].reason = "quietly rewritten"
    report = LedgerVerifier.verify(events)
    assert report["valid"] is False
    assert ALTERED_EVENT in report["codes"]
    assert any(i["sequence"] == 3 for i in report["issues"] if i["code"] == ALTERED_EVENT)


def test_verifier_detects_missing_event(store):
    events = _seed(store, 5)
    report = LedgerVerifier.verify(events[:2] + events[3:])
    assert report["valid"] is False
    assert MISSING_EVENT in report["codes"]


def test_verifier_detects_reordered_event(store):
    events = _seed(store, 4)
    swapped = [events[0], events[2], events[1], events[3]]
    report = LedgerVerifier.verify(swapped)
    assert report["valid"] is False
    assert REORDERED_EVENT in report["codes"]


def test_verifier_detects_broken_previous_hash(store):
    events = _seed(store, 3)
    events[2].previous_hash = "b" * 64
    events[2].seal()  # Re-seal: the content hash is consistent, the link is not.
    report = LedgerVerifier.verify(events)
    assert report["valid"] is False
    assert BROKEN_PREVIOUS_HASH in report["codes"]


def test_verifier_detects_duplicate_sequence(store):
    events = _seed(store, 3)
    events[2].sequence = 2
    events[2].seal()
    report = LedgerVerifier.verify(events)
    assert report["valid"] is False
    assert DUPLICATE_SEQUENCE in report["codes"]


def test_verifier_detects_unsupported_schema(store):
    events = _seed(store, 3)
    events[1].schema_version = 99
    report = LedgerVerifier.verify(events)
    assert report["valid"] is False
    assert UNSUPPORTED_SCHEMA in report["codes"]


def test_verifier_detects_truncation_at_the_head(store):
    events = _seed(store, 4)
    # Dropping the tail is undetectable without an external anchor; dropping
    # the head is not, because the chain no longer starts at genesis.
    report = LedgerVerifier.verify(events[1:])
    assert report["valid"] is False


def test_tampering_in_an_export_file_is_detected(store, tmp_path):
    _seed(store, 3)
    path = tmp_path / "ledger.jsonl"
    store.export_jsonl(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["reason"] = "tampered"
    lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = EvidenceLedger.verify_export(path)
    assert report["valid"] is False
    assert ALTERED_EVENT in report["codes"]


# ── F1: product actions ──


def test_registration_and_capability_change_are_recorded(ledger):
    ledger.record_repository_registered(
        "repo-1", actor_identity="Alice", trust_level="trusted", source_revision="abc123"
    )
    ledger.record_capability_changed(
        "repo-1",
        actor_identity="alice",
        capability="apply_candidate_patches",
        previous_state="denied",
        new_state="granted",
        reason="reviewed",
    )
    history = ledger.entity_history("capability", "repo-1:apply_candidate_patches")
    assert history["total"] == 1
    assert history["events"][0]["new_state"] == "granted"
    assert ledger.verify()["valid"] is True


def test_plan_decision_is_always_attributed_to_a_human(ledger):
    event = ledger.record_remediation_plan_decision(
        "repo-1", "plan-1", approved=True, actor_identity="alice", reason="reviewed"
    )
    assert event.event_type == EventType.REMEDIATION_PLAN_APPROVED.value
    assert event.actor_type == ActorType.HUMAN.value


def test_export_event_states_it_did_not_touch_the_repository(ledger):
    event = ledger.record_patch_exported(
        "repo-1",
        "exp-out-1",
        experiment_id="exp-1",
        option_id="opt-1",
        patch_hash="c" * 64,
        actor_identity="alice",
    )
    assert event.metadata["applied_to_repository"] is False


def test_validation_event_references_output_without_embedding_it(ledger):
    event = ledger.record_validation_command_executed(
        "repo-1",
        "ws-1",
        command=["python", "-m", "compileall", "-q", "."],
        exit_code=0,
        duration_seconds=1.234,
        profile_name="python-compile",
        artifact_references=["lab/sessions/lab-1.json"],
    )
    assert event.metadata["argv"][0] == "python"
    assert event.metadata["network_isolation"] == "unverified"
    assert event.artifact_references == ["lab/sessions/lab-1.json"]
    assert "stdout" not in event.metadata


def test_full_engineering_lifecycle_stays_verifiable(ledger):
    ledger.record_repository_registered("repo-1", actor_identity="alice")
    ledger.record_graph_generation_built("repo-1", "gen-1", source_revision="rev1")
    ledger.record_graph_generation_activated("repo-1", "gen-1")
    ledger.record_portfolio_generation_activated("portfolio-1", "pgen-1")
    ledger.record_finding_created("repo-1", "find-1", severity="critical")
    ledger.record_remediation_plan_proposed("repo-1", "plan-1", finding_id="find-1")
    ledger.record_remediation_plan_decision(
        "repo-1", "plan-1", approved=True, actor_identity="alice"
    )
    ledger.record_workspace_created("repo-1", "ws-1", base_revision="rev1")
    ledger.record_patch_applied_in_workspace(
        "repo-1", "ws-1", patch_id="patch-1", patch_hash="d" * 64
    )
    ledger.record_validation_command_executed(
        "repo-1", "ws-1", command=["python", "-V"], exit_code=0, duration_seconds=0.1
    )
    ledger.record_experiment_completed(
        "repo-1", "exp-1", conclusion="recommend_option", recommended_option_id="opt-1"
    )
    ledger.record_recommendation_acknowledged(
        "repo-1", "exp-1", actor_identity="alice", action="accept_for_export"
    )
    ledger.record_patch_exported(
        "repo-1",
        "exp-out-1",
        experiment_id="exp-1",
        option_id="opt-1",
        patch_hash="e" * 64,
        actor_identity="alice",
    )
    ledger.record_human_marked_patch_applied(
        "repo-1", "exp-out-1", actor_identity="alice", applied_revision="rev2"
    )
    ledger.record_verification("repo-1", "exp-out-1", passed=True, actor_identity="alice")
    ledger.record_workspace_state(
        "repo-1", "ws-1", previous_state="passed", new_state="cleaned"
    )
    ledger.record_artifact_superseded(
        "repo-1", "gen-1", entity_type="graph_generation", superseded_by="gen-2"
    )

    report = ledger.verify()
    assert report["valid"] is True
    assert report["events_checked"] == 17
    workspace = ledger.entity_history("workspace", "ws-1")
    assert workspace["total"] == 4


# ── F3/F4: retention by tombstone ──


def test_redaction_appends_a_tombstone_and_keeps_the_original(ledger):
    target = ledger.record_finding_created("repo-1", "find-1", severity="high")
    tombstone = ledger.record_redaction(
        target_event_id=target.event_id,
        actor_identity="alice",
        reason="customer name appeared in the finding title",
        redacted_fields=["metadata.title"],
    )
    assert tombstone.event_type == EventType.REDACTION_RECORDED.value
    assert tombstone.metadata["target_event_hash"] == target.event_hash
    assert tombstone.metadata["history_preserved"] is True
    # The referenced event is untouched, so the chain still verifies.
    assert ledger.get(target.event_id).event_hash == target.event_hash
    assert ledger.verify()["valid"] is True


def test_redaction_of_an_unknown_event_is_refused(ledger):
    with pytest.raises(LedgerIntegrityError):
        ledger.record_redaction(
            target_event_id="evt-missing",
            actor_identity="alice",
            reason="x",
            redacted_fields=[],
        )


def test_ledger_exposes_no_update_or_delete_api():
    forbidden = {"update", "delete", "remove", "purge", "rewrite", "amend"}
    for name in dir(EvidenceLedger) + dir(LedgerStore):
        if name.startswith("_"):
            continue
        assert not (forbidden & set(name.split("_"))), f"{name} mutates ledger history"


def test_health_summarizes_integrity(ledger):
    ledger.record_finding_created("repo-1", "find-1", severity="low")
    health = ledger.health()
    assert health["events"] == 1 and health["valid"] is True
    assert health["head_sequence"] == 1 and health["head_hash"]


# ── F5: CLI ──


def test_cli_verify_export_show_and_history(tmp_path, capsys):
    brain_dir = tmp_path / ".brain"
    ledger = EvidenceLedger(brain_dir)
    created = ledger.record_finding_created("repo-1", "find-1", severity="critical")
    ledger.record_workspace_created("repo-1", "ws-1", base_revision="rev1")

    assert ledger_cli(["--brain-dir", str(brain_dir), "verify"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

    output = tmp_path / "ledger.jsonl"
    assert ledger_cli(["--brain-dir", str(brain_dir), "export", "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["events"] == 2
    assert output.is_file()

    assert ledger_cli(["--brain-dir", str(brain_dir), "show", created.event_id]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["entity_id"] == "find-1" and shown["hash_valid"] is True

    assert (
        ledger_cli(["--brain-dir", str(brain_dir), "entity-history", "workspace", "ws-1"]) == 0
    )
    history = json.loads(capsys.readouterr().out)
    assert history["total"] == 1 and history["entity_id"] == "ws-1"


def test_cli_show_unknown_event_fails(tmp_path, capsys):
    brain_dir = tmp_path / ".brain"
    EvidenceLedger(brain_dir)
    assert ledger_cli(["--brain-dir", str(brain_dir), "show", "evt-nope"]) == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_cli_verify_fails_the_shell_on_a_tampered_export(tmp_path, capsys):
    brain_dir = tmp_path / ".brain"
    ledger = EvidenceLedger(brain_dir)
    ledger.record_finding_created("repo-1", "find-1", severity="low")
    ledger.record_finding_created("repo-1", "find-2", severity="low")
    path = tmp_path / "ledger.jsonl"
    ledger.export(path)

    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["reason"] = "tampered"
    lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert ledger_cli(["--brain-dir", str(brain_dir), "verify", "--input", str(path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["valid"] is False and ALTERED_EVENT in report["codes"]


def test_cli_events_query_is_paginated(tmp_path, capsys):
    brain_dir = tmp_path / ".brain"
    ledger = EvidenceLedger(brain_dir)
    for index in range(7):
        ledger.record_finding_created("repo-1", f"find-{index}", severity="low")
    assert ledger_cli(["--brain-dir", str(brain_dir), "events", "--limit", "3"]) == 0
    page = json.loads(capsys.readouterr().out)
    assert page["returned"] == 3 and page["total"] == 7 and page["has_more"] is True


def test_cli_verify_missing_export_reports_an_error(tmp_path, capsys):
    brain_dir = tmp_path / ".brain"
    EvidenceLedger(brain_dir)
    assert (
        ledger_cli(
            ["--brain-dir", str(brain_dir), "verify", "--input", str(tmp_path / "nope.jsonl")]
        )
        == 1
    )
    assert "error" in json.loads(capsys.readouterr().out)


# ── F6: API surface ──


def test_ledger_router_is_mounted_with_the_required_endpoints():
    from apps.api.main import app

    paths = set(app.openapi()["paths"])
    assert {
        "/ledger/events",
        "/ledger/events/{event_id}",
        "/ledger/entities/{entity_type}/{entity_id}",
        "/ledger/verify",
    } <= paths


def test_ledger_api_is_read_only():
    from apps.api.main import app

    writes = {"post", "put", "patch", "delete"}
    schema = app.openapi()["paths"]
    ledger_paths = [p for p in schema if p.startswith("/ledger")]
    assert ledger_paths
    for path in ledger_paths:
        assert not (writes & set(schema[path])), f"{path} exposes a write method"


def test_ledger_endpoints_require_authentication():
    from apps.api.routers import ledger as ledger_router

    assert ledger_router.router.dependencies, "ledger router must require an API key"


def test_ledger_page_size_is_capped_in_the_api():
    from apps.api.main import app

    parameters = app.openapi()["paths"]["/ledger/events"]["get"]["parameters"]
    limit = next(p for p in parameters if p["name"] == "limit")
    assert limit["schema"]["maximum"] == MAX_PAGE_SIZE


def test_ledger_module_documents_the_non_autonomy_boundary():
    import brain.ledger as package

    text = Path(package.__file__).read_text(encoding="utf-8")
    assert (
        "It does not apply patches to authoritative repositories, commit changes, "
        "push branches, merge pull requests, or deploy software." in " ".join(text.split())
    )
