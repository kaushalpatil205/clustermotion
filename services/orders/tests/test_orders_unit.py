"""Unit tests that need no database (lifespan is not started)."""
from fastapi.testclient import TestClient

from app import main, sweeper
from shared.lease_guard import LeaseGuard

client = TestClient(main.app)


def test_idempotency_key_is_required():
    resp = client.post("/api/orders", json={"sku": "sku-100", "qty": 1})
    assert resp.status_code == 422


def test_payload_validation():
    resp = client.post("/api/orders", json={"sku": "", "qty": 0},
                       headers={"Idempotency-Key": "abcdefgh"})
    assert resp.status_code == 422


def test_returns_503_until_schema_ready():
    main.schema_ready.clear()
    resp = client.post("/api/orders", json={"sku": "sku-100", "qty": 1},
                       headers={"Idempotency-Key": "abcdefgh"})
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "1"


def test_slot_is_aligned():
    assert sweeper.current_slot(1_000_000_123) % sweeper.SLOT_SECONDS == 0


class FakeDynamo:
    def __init__(self, holder):
        self.holder = holder
        self.calls = 0

    def get_item(self, **_):
        self.calls += 1
        if self.holder is None:
            raise RuntimeError("dynamodb down")
        return {"Item": {"lease_id": {"S": "singletons"}, "holder": {"S": self.holder}}}


def test_lease_guard_holder_and_cache():
    fake = FakeDynamo("blue")
    guard = LeaseGuard(table="t", cluster="blue", cache_seconds=60, client=fake)
    assert guard.holds_lease() and guard.holds_lease()
    assert fake.calls == 1  # second answer came from the cache


def test_lease_guard_other_holder():
    guard = LeaseGuard(table="t", cluster="green", client=FakeDynamo("blue"))
    assert guard.holds_lease() is False


def test_lease_guard_fails_closed():
    guard = LeaseGuard(table="t", cluster="blue", client=FakeDynamo(None))
    assert guard.holds_lease() is False
