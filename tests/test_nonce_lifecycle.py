"""Nonce rejection must release the pending challenge record as well."""
from brain.harness.nonce import RemoteNonceManager


def test_consuming_nonce_evicts_its_pending_record():
    manager = RemoteNonceManager()
    nonce = manager.generate_nonce()
    assert manager.verify_and_consume(nonce)
    assert nonce not in manager._issued_nonces
    assert not manager.verify_and_consume(nonce)


def test_expired_nonce_evicts_its_pending_record(monkeypatch):
    import brain.harness.nonce as module

    now = [100.0]
    monkeypatch.setattr(module, "_elapsed_time", lambda: now[0])
    manager = RemoteNonceManager(ttl_seconds=10)
    nonce = manager.generate_nonce()
    now[0] = 111.0
    assert not manager.verify_and_consume(nonce)
    assert nonce not in manager._issued_nonces
    assert not manager.verify_and_consume(nonce)
