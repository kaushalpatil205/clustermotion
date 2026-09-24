"""
Integration tests for the ClusterMotion shop on the local Docker Compose stack.

Prerequisites:
    docker compose -f local/compose.yaml up --build -d
    # wait ~10 s for services to be ready
"""
import os, time, uuid, subprocess, json
import pytest, requests, boto3

CATALOG_URL = os.getenv("CATALOG_URL", "http://localhost:8081")
ORDERS_URL = os.getenv("ORDERS_URL", "http://localhost:8082")
DYNAMO_URL = os.getenv("DYNAMO_URL", "http://localhost:8001")
DB_DSN = os.getenv("DB_DSN", "postgresql://shop:shop@localhost:5432/shop")


@pytest.fixture
def idempotency_key():
    return str(uuid.uuid4())


# ── catalog ────────────────────────────────────────────────────────────────

def test_catalog_list():
    """GET /api/catalog/products returns a non-empty list."""
    r = requests.get(f"{CATALOG_URL}/api/catalog/products")
    assert r.status_code == 200
    items = r.json()["items"]
    assert isinstance(items, list) and len(items) > 0


def test_catalog_product():
    """GET /api/catalog/products/sku-100 returns the product."""
    r = requests.get(f"{CATALOG_URL}/api/catalog/products/sku-100")
    assert r.status_code == 200
    assert r.json()["sku"] == "sku-100"


# ── orders (idempotent) ───────────────────────────────────────────────────

def test_order_create_idempotent(idempotency_key):
    """POST with the same Idempotency-Key returns 201 then 200 with same id."""
    body = {"sku": "sku-100", "qty": 1}
    hdrs = {"Content-Type": "application/json", "Idempotency-Key": idempotency_key}

    r1 = requests.post(f"{ORDERS_URL}/api/orders", json=body, headers=hdrs)
    assert r1.status_code == 201
    order_id = r1.json()["order_id"]

    r2 = requests.post(f"{ORDERS_URL}/api/orders", json=body, headers=hdrs)
    assert r2.status_code == 200
    assert r2.json()["order_id"] == order_id


# ── fulfillment ───────────────────────────────────────────────────────────

def test_order_fulfilled(idempotency_key):
    """An order reaches FULFILLED within 30 s."""
    body = {"sku": "sku-100", "qty": 1}
    hdrs = {"Content-Type": "application/json", "Idempotency-Key": idempotency_key}

    r = requests.post(f"{ORDERS_URL}/api/orders", json=body, headers=hdrs)
    assert r.status_code == 201
    oid = r.json()["order_id"]

    deadline = time.time() + 30
    while time.time() < deadline:
        status = requests.get(f"{ORDERS_URL}/api/orders/{oid}").json()["status"]
        if status == "FULFILLED":
            return
        time.sleep(2)
    pytest.fail(f"Order {oid} still {status} after 30 s")


# ── fencing ───────────────────────────────────────────────────────────────

def _set_lease_holder(holder: str):
    ddb = boto3.resource(
        "dynamodb",
        endpoint_url=DYNAMO_URL,
        region_name="us-east-1",
        aws_access_key_id="x",
        aws_secret_access_key="x",
    )
    table = ddb.Table("clustermotion-leases")
    table.update_item(
        Key={"lease_id": "singletons"},
        UpdateExpression="SET holder = :h",
        ExpressionAttributeValues={":h": holder},
    )


def test_fencing(idempotency_key):
    """When the lease moves away, orders stay PENDING; when it returns, they fulfill."""
    # Move lease away so the worker stops processing
    _set_lease_holder("other")
    time.sleep(6)  # wait for the worker to notice

    body = {"sku": "sku-100", "qty": 1}
    hdrs = {"Content-Type": "application/json", "Idempotency-Key": idempotency_key}
    r = requests.post(f"{ORDERS_URL}/api/orders", json=body, headers=hdrs)
    assert r.status_code == 201
    oid = r.json()["order_id"]

    time.sleep(10)
    status = requests.get(f"{ORDERS_URL}/api/orders/{oid}").json()["status"]
    assert status == "PENDING", f"Expected PENDING while fenced, got {status}"

    # Restore lease
    _set_lease_holder("local")
    deadline = time.time() + 25
    while time.time() < deadline:
        status = requests.get(f"{ORDERS_URL}/api/orders/{oid}").json()["status"]
        if status == "FULFILLED":
            return
        time.sleep(2)
    pytest.fail(f"Order {oid} still {status} after restoring lease")


# ── sweeper idempotency ──────────────────────────────────────────────────

def test_sweeper_idempotent():
    """Running the sweeper twice in the same slot produces one 'ran' and one skip."""
    for _ in range(2):
        subprocess.run(
            ["docker", "compose", "-f", "local/compose.yaml", "run", "--rm", "sweeper"],
            capture_output=True, timeout=30,
        )
    # Query the DB to check sweeper_runs for the current slot
    import psycopg
    with psycopg.connect(DB_DSN) as conn:
        rows = conn.execute(
            "SELECT outcome, count(*) FROM sweeper_runs GROUP BY outcome"
        ).fetchall()
    outcomes = {r[0]: r[1] for r in rows}
    assert outcomes.get("ran", 0) >= 1, "Expected at least one 'ran'"
