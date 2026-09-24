from fastapi.testclient import TestClient

from app import main

client = TestClient(main.app)


def test_healthz():
    assert client.get("/api/catalog/healthz").json() == {"status": "ok"}


def test_list_is_sorted_and_stamped():
    body = client.get("/api/catalog/products").json()
    skus = [item["sku"] for item in body["items"]]
    assert skus == sorted(skus)
    assert "generated_at" in body


def test_unknown_sku_is_404():
    assert client.get("/api/catalog/products/nope").status_code == 404


def test_served_by_header_not_in_body():
    resp = client.get("/api/catalog/products/sku-100")
    assert resp.headers["X-Served-By"] == main.CLUSTER
    assert "cluster" not in resp.json()


def test_price_bug_switch(monkeypatch):
    monkeypatch.setattr(main, "FAULT_PRICE_BUG", True)
    assert client.get("/api/catalog/products/sku-100").json()["price_cents"] == 13000


def test_error_injection(monkeypatch):
    monkeypatch.setattr(main, "FAULT_ERROR_RATE", 1.0)
    assert client.get("/api/catalog/products").status_code == 500
    assert client.get("/api/catalog/healthz").status_code == 200  # health never faulted
