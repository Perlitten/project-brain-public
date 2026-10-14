"""Force preemption inside state transitions; no probabilistic stress-only claims."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import pickle
import threading

from brain.context.budget import ContextBudget, bounded_join
from brain.harness.attestation import create_attestation
from brain.harness.models import ClaimCategory, EvidenceAuthority, EvidenceState, ReportClaim
from brain.harness.nonce import RemoteNonceManager
from brain.harness.verifier import verify_claim


def _run(role, local, entered, callback):
    local.role = role
    entered.set()
    return callback()


def test_simultaneous_nonce_consumers_return_exactly_one_success(monkeypatch):
    import brain.harness.nonce as module

    local = threading.local()
    inside, second_inside, second_started, release = (threading.Event() for _ in range(4))

    def clock():
        if getattr(local, "role", "") == "first":
            inside.set()
            assert release.wait(2)
        elif getattr(local, "role", "") == "second":
            second_inside.set()
        return 100.0

    monkeypatch.setattr(module, "_elapsed_time", clock)
    manager = RemoteNonceManager()
    nonce = manager.generate_nonce()
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(_run, "first", local, threading.Event(), lambda: manager.verify_and_consume(nonce))
        try:
            assert inside.wait(2)
            second = pool.submit(_run, "second", local, second_started, lambda: manager.verify_and_consume(nonce))
            assert second_started.wait(2)
            second_inside.wait(0.1)
        finally:
            release.set()
        assert sorted([first.result(2), second.result(2)]) == [False, True]
    assert nonce not in manager._issued_nonces


def test_nonce_generation_cleanup_cannot_race_with_consumption(monkeypatch):
    import brain.harness.nonce as module

    local = threading.local()
    inside, second_done, second_started, release = (threading.Event() for _ in range(4))
    monkeypatch.setattr(module, "_elapsed_time", lambda: 100.0)
    manager = RemoteNonceManager()
    nonce = manager.generate_nonce()

    class PausedRecords(dict):
        def items(self):
            for item in super().items():
                yield item
                if getattr(local, "role", "") == "generator":
                    inside.set()
                    assert release.wait(2)

    manager._issued_nonces = PausedRecords(manager._issued_nonces)

    def consume():
        try:
            return manager.verify_and_consume(nonce)
        finally:
            second_done.set()

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(_run, "generator", local, threading.Event(), manager.generate_nonce)
        try:
            assert inside.wait(2)
            second = pool.submit(_run, "consumer", local, second_started, consume)
            assert second_started.wait(2)
            second_done.wait(0.1)
        finally:
            release.set()
        fresh = first.result(2)
        assert second.result(2)
    assert manager.verify_and_consume(fresh)
    assert not manager.verify_and_consume(nonce)


def test_parallel_section_reservations_cannot_overbook_bytes(monkeypatch):
    import brain.context.budget as module

    local = threading.local()
    inside, second_inside, second_started, release = (threading.Event() for _ in range(4))
    truncate = module.truncate_utf8

    def paused(value, cap, suffix="…"):
        if getattr(local, "role", "") == "first":
            inside.set()
            assert release.wait(2)
        else:
            second_inside.set()
        return truncate(value, cap, suffix)

    monkeypatch.setattr(module, "truncate_utf8", paused)
    budget = ContextBudget(10, allocations={"shared": 8})
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(_run, "first", local, threading.Event(), lambda: budget.add_text("shared", "abcdef"))
        try:
            assert inside.wait(2)
            second = pool.submit(_run, "second", local, second_started, lambda: budget.add_text("shared", "ghijkl"))
            assert second_started.wait(2)
            second_inside.wait(0.1)
        finally:
            release.set()
        values = [first.result(2), second.result(2)]
    actual = sum(len(value.encode()) for value in values)
    assert actual <= 8
    assert budget.sections == {"shared": actual}
    assert budget.consumed_bytes == actual
    assert budget.remaining_bytes == 10 - actual


def test_parallel_claim_verification_does_not_publish_mixed_outcomes(monkeypatch):
    local = threading.local()
    inside, second_done, second_started, release = (threading.Event() for _ in range(4))
    attestation = create_attestation("race", "local", ["run"], 0, "passed", "", "start", "end", 1, image_id="image")
    invalid = attestation.model_copy(deep=True)
    invalid.exit_code = 1
    invalid.attestation_hash = invalid.calculate_hash()
    claim = ReportClaim(claim_id="race", claim_text="Run", claim_category=ClaimCategory.INTEGRITY,
                        required_evidence_types=[EvidenceAuthority.LOCAL_EXECUTOR],
                        supplied_attestation_ids=[attestation.attestation_id])
    original = ReportClaim.__setattr__

    def paused(self, name, value):
        original(self, name, value)
        if self is claim and name == "confidence" and value == 1.0 and getattr(local, "role", "") == "first":
            inside.set()
            assert release.wait(2)

    monkeypatch.setattr(ReportClaim, "__setattr__", paused)

    def reject():
        try:
            return verify_claim(claim, [invalid])
        finally:
            second_done.set()

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(_run, "first", local, threading.Event(), lambda: verify_claim(claim, [attestation]))
        try:
            assert inside.wait(2)
            second = pool.submit(_run, "second", local, second_started, reject)
            assert second_started.wait(2)
            second_done.wait(0.1)
        finally:
            release.set()
        assert first.result(2) == EvidenceState.VERIFIED
        assert second.result(2) == EvidenceState.FAILED
    assert claim.verification_status == EvidenceState.FAILED
    assert claim.confidence == 0.0
    assert not claim.allowed_in_executive_summary


def test_join_admission_and_reservation_are_one_operation():
    inside, second_done, second_started, release = (threading.Event() for _ in range(4))
    local = threading.local()
    budget = ContextBudget(5)

    def values():
        yield "ab"
        inside.set()
        assert release.wait(2)
        yield "cd"

    def reserve():
        try:
            return budget.add_text("other", "xxxx")
        finally:
            second_done.set()

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(lambda: bounded_join(budget, "joined", values(), "|"))
        try:
            assert inside.wait(2)
            second = pool.submit(_run, "second", local, second_started, reserve)
            assert second_started.wait(2)
            second_done.wait(0.1)
        finally:
            release.set()
        joined, other = first.result(2), second.result(2)
    assert joined in ("", "ab", "ab|cd")  # Never a partial admitted item.
    assert len((joined + other).encode()) <= 5


def test_generating_challenges_evicts_expired_unused_records(monkeypatch):
    import brain.harness.nonce as module

    now = [100.0]
    monkeypatch.setattr(module, "_elapsed_time", lambda: now[0])
    manager = RemoteNonceManager(ttl_seconds=10)
    old = [manager.generate_nonce() for _ in range(20)]
    now[0] = 111.0
    current = manager.generate_nonce()
    assert set(manager._issued_nonces) == {current}
    assert all(not manager.verify_and_consume(nonce) for nonce in old)


def test_replay_markers_expire_without_resurrecting_a_spent_challenge(monkeypatch):
    import brain.harness.nonce as module

    now = [100.0]
    monkeypatch.setattr(module, "_elapsed_time", lambda: now[0])
    manager = RemoteNonceManager(ttl_seconds=10)
    nonce = manager.generate_nonce()
    assert manager.verify_and_consume(nonce)
    now[0] = 111.0
    manager.generate_nonce()
    assert not manager._consumed_nonces
    assert not manager.verify_and_consume(nonce)


def test_cleanup_preserves_live_challenges_and_replay_markers_at_exact_ttl(monkeypatch):
    import brain.harness.nonce as module

    now = [100.0]
    monkeypatch.setattr(module, "_elapsed_time", lambda: now[0])
    manager = RemoteNonceManager(ttl_seconds=10)
    pending, spent = manager.generate_nonce(), manager.generate_nonce()
    assert manager.verify_and_consume(spent)
    now[0] = 110.0
    manager.generate_nonce()
    assert spent in manager._consumed_nonces
    assert manager.verify_and_consume(pending)
    assert not manager.verify_and_consume(spent)


def test_budget_deepcopy_keeps_independent_accounting():
    budget = ContextBudget(10)
    budget.add_text("code", "abc")
    copied = deepcopy(budget)
    copied.add_text("code", "def")
    assert copied.consumed_bytes == 6
    assert budget.consumed_bytes == 3


def test_snapshot_exclusion_records_do_not_alias_mutable_state():
    budget = ContextBudget(10)
    budget.exclude("code", "byte_cap")
    snapshot = budget.snapshot()
    snapshot["excluded"][0]["reason"] = "changed"
    assert budget.snapshot()["excluded"] == [{"section": "code", "reason": "byte_cap"}]


def test_snapshot_cannot_observe_half_committed_reservation(monkeypatch):
    budget = ContextBudget(10)
    inside, second_done, second_started, release = (threading.Event() for _ in range(4))
    local = threading.local()
    original = ContextBudget.__setattr__

    def paused(self, name, value):
        original(self, name, value)
        if self is budget and name == "consumed_bytes" and value == 3:
            inside.set()
            assert release.wait(2)

    def snapshot():
        try:
            return budget.snapshot()
        finally:
            second_done.set()

    monkeypatch.setattr(ContextBudget, "__setattr__", paused)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(budget.add_text, "code", "abc")
        try:
            assert inside.wait(2)
            second = pool.submit(_run, "reader", local, second_started, snapshot)
            assert second_started.wait(2)
            second_done.wait(0.1)
        finally:
            release.set()
        assert first.result(2) == "abc"
        state = second.result(2)
    assert state["bytes"] == sum(state["sections"].values()) == 3


def test_parallel_exclusions_cannot_overflow_metadata_cap():
    budget = ContextBudget(10)
    local = threading.local()
    inside, second_done, second_started, release = (threading.Event() for _ in range(4))

    class PausedList(list):
        def __len__(self):
            size = super().__len__()
            if getattr(local, "role", "") == "first":
                inside.set()
                assert release.wait(2)
            return size

    budget.exclusions = PausedList([{"section": "code", "reason": "byte_cap"} for _ in range(23)])

    def exclude():
        try:
            budget.exclude("code", "byte_cap")
        finally:
            second_done.set()

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(_run, "first", local, threading.Event(), exclude)
        try:
            assert inside.wait(2)
            second = pool.submit(_run, "second", local, second_started, exclude)
            assert second_started.wait(2)
            second_done.wait(0.1)
        finally:
            release.set()
        first.result(2)
        second.result(2)
    assert len(budget.snapshot()["excluded"]) == 24


def test_claim_copy_and_serialization_do_not_expose_or_share_locks():
    claim = ReportClaim(claim_id="copy", claim_text="run", claim_category=ClaimCategory.INTEGRITY,
                        required_evidence_types=[])
    assert not any("lock" in name for name in ReportClaim.model_json_schema()["properties"])
    for copied in (claim.model_copy(deep=True), pickle.loads(pickle.dumps(claim))):
        assert copied.model_dump() == claim.model_dump()
        assert copied == claim
        assert "lock" not in copied.model_dump_json()
        assert copied._verification_lock is not claim._verification_lock
        assert verify_claim(copied, []) == EvidenceState.UNVERIFIABLE


def test_independent_claims_can_be_verified_while_another_claim_is_locked():
    claim = ReportClaim(claim_id="held", claim_text="run", claim_category=ClaimCategory.INTEGRITY,
                        required_evidence_types=[])
    independent = claim.model_copy(deep=True)
    with ThreadPoolExecutor(1) as pool, claim._verification_lock:
        assert pool.submit(verify_claim, independent, []).result(2) == EvidenceState.UNVERIFIABLE
