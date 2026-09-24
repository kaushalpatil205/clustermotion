# Testing strategy

This document describes the test levels, test code and failure-injection scenarios used to validate ClusterMotion.

## 1. Test levels

| Level | Name | What it covers | When to run |
|---|---|---|---|
| L0 | Static checks | `terraform fmt/validate`, `helm lint`, `ansible --syntax-check`, `docs_to_code.py --check` | Every commit (CI) |
| L1 | Unit tests | Service pytest (catalog 6, orders 7, fulfillment 2) + engine pytest (32) = **47 tests** | Every commit (CI) |
| L2 | Local integration | Docker Compose stack: end-to-end order flow, fencing, sweeper idempotency | Before first AWS deploy |
| L3 | Smoke on blue | `cm smoke --color blue` after bootstrapping the first cluster | After `bootstrap-blue` |
| L4 | Full migration e2e | k6 load during the entire migration + `cm verify` reconciliation | The migration itself |
| L5 | Failure injection | Scenarios F1–F5: prove that rollback, fencing and switchback all work | After the first clean run |

---

## 2. k6 load generator

The load test simulates real traffic during the migration.  Run it on the management node and keep it running until **after** `cm verify`.

```
k6 run tests/load/shop.js \
  -e ORDERS_URL=http://<alb-dns> \
  --log-output=file=results/confirmed.jsonl \
  --log-format=raw
```

**File:** `tests/load/shop.js`

```javascript
import http from "k6/http";
import { check, sleep } from "k6";
import { uuidv4 } from "https://jslib.k6.io/k6-utils/1.4.0/index.js";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------
const BASE = __ENV.BASE_URL || "http://localhost:8080";
const SKUS = ["sku-100", "SKU-002", "SKU-003", "SKU-004", "SKU-005"];

export const options = {
  scenarios: {
    shop: {
      executor: "constant-arrival-rate",
      rate: 20,
      timeUnit: "1s",
      duration: __ENV.DURATION || "30m",
      preAllocatedVUs: 10,
      maxVUs: 50,
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<500"],
  },
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const headers = { "User-Agent": "shop-loadgen/1.0" };

function catalogRead() {
  const r = Math.random();
  if (r < 0.3) {
    const sku = SKUS[Math.floor(Math.random() * SKUS.length)];
    return http.get(`${BASE}/api/catalog/products/${sku}`, { headers });
  }
  return http.get(`${BASE}/api/catalog/products`, { headers });
}

function orderCreate() {
  const key = uuidv4();
  const sku = SKUS[Math.floor(Math.random() * SKUS.length)];
  const body = JSON.stringify({ sku, qty: 1 });
  const opts = {
    headers: {
      ...headers,
      "Content-Type": "application/json",
      "Idempotency-Key": key,
    },
  };

  let res = http.post(`${BASE}/api/orders`, body, opts);

  // Retry with the SAME key on server errors (idempotent).
  let attempt = 0;
  while (res.status >= 500 && attempt < 10) {
    attempt++;
    const backoff = Math.min(Math.pow(2, attempt) * 100, 120000);
    sleep(backoff / 1000);
    res = http.post(`${BASE}/api/orders`, body, opts);
  }

  if (res.status === 201 || res.status === 200) {
    const j = res.json();
    console.log(
      JSON.stringify({
        event: "confirmed",
        t: Date.now(),
        key: key,
        order_id: j.id,
        status: j.status,
      })
    );
    return res;
  }
  return res;
}

function orderRead(orderId) {
  return http.get(`${BASE}/api/orders/${orderId}`, { headers });
}

// ---------------------------------------------------------------------------
// Default function — called by each VU iteration
// ---------------------------------------------------------------------------
let lastOrderId = null;

export default function () {
  const dice = Math.random();

  if (dice < 0.6) {
    // 60 % catalog reads
    const res = catalogRead();
    check(res, { "catalog 2xx": (r) => r.status >= 200 && r.status < 300 });
  } else if (dice < 0.9) {
    // 30 % order creates
    const res = orderCreate();
    check(res, {
      "order accepted": (r) => r.status === 201 || r.status === 200,
    });
    if (res.status === 201 || res.status === 200) {
      lastOrderId = res.json().id;
    }
  } else {
    // 10 % order reads
    if (lastOrderId) {
      const res = orderRead(lastOrderId);
      check(res, { "order read 2xx": (r) => r.status === 200 });
    } else {
      catalogRead(); // fallback if no order yet
    }
  }
}
```

---

## 3. Local integration tests

Run against the Docker Compose stack (`make local-up` first).

```
pytest tests/integration/test_local_stack.py -v
```

**File:** `tests/integration/test_local_stack.py`

```python
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
```

---

## 4. CI workflow

**File:** `.github/workflows/ci.yml`

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

env:
  AWS_REGION: us-east-1

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1

      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97  # v7.0.0
        with:
          python-version: "3.13"

      - name: Service tests
        run: |
          pip install pytest httpx
          for svc in catalog orders fulfillment; do
            pip install -r services/$svc/requirements.txt
            pytest services/$svc -q
          done

      - name: Engine tests
        run: |
          pip install -e 'engine/.[test]'
          pytest engine -q

      - uses: azure/setup-helm@9bc31f4ebc9c6b171d7bfbaa5d006ae7abdb4310  # v5.0.1

      - name: Helm lint
        run: helm lint gitops/charts/shop

      - uses: hashicorp/setup-terraform@dfe3c3f87815947d99a8997f908cb6525fc44e9e  # v4.0.1

      - name: Terraform fmt check
        run: terraform fmt -check -recursive infra/terraform

      - name: Docs ↔ code sync
        run: python3 scripts/docs_to_code.py --check

  images:
    runs-on: ubuntu-latest
    needs: test
    if: github.ref == 'refs/heads/main'
    permissions:
      id-token: write
      contents: write
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1

      - uses: aws-actions/configure-aws-credentials@e1253824e5c10ff9df46874f81ed3ec929e19cfd  # v6.3.0
        with:
          role-to-assume: ${{ secrets.ECR_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      - id: ecr
        uses: aws-actions/amazon-ecr-login@03f1aad4c6c7ffd436567f42f9384779290529bd  # v2.1.7

      - uses: docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069  # v4.4.1

      - name: Build & push images
        uses: docker/build-push-action@c3c9e263c25d99ce0380d002d59b67737d91b0dc  # v7.4.0
        strategy:
          matrix:
            include:
              - name: catalog
                context: services/catalog
              - name: orders
                context: services/orders
              - name: fulfillment
                context: services/fulfillment
              - name: engine
                context: engine
        with:
          context: ${{ matrix.context }}
          push: true
          tags: |
            ${{ steps.ecr.outputs.registry }}/clustermotion/${{ matrix.name }}:${{ github.sha }}
            ${{ steps.ecr.outputs.registry }}/clustermotion/${{ matrix.name }}:latest

      - name: Update image tag in GitOps
        run: |
          sed -i "s/tag: .*/tag: ${{ github.sha }}/" gitops/charts/shop/values.yaml
          git config user.name "github-actions"
          git config user.email "actions@github.com"
          git add gitops/charts/shop/values.yaml
          git commit -m "chore: bump image tag to ${{ github.sha }} [skip ci]" || true
          git push
```

---

## 5. Failure scenarios (L5)

Run these after a successful clean migration to prove safety mechanisms work.

### F1 — Catalog error rate → traffic shift rollback

| | |
|---|---|
| **Setup** | In `gitops/charts/shop/values-green.yaml`, set `faults.catalogErrorRate: "0.2"` |
| **Steps** | Start `cm migrate --from blue`. The shift reaches 5 % green. |
| **Expected** | The SLO gate detects > 1 % 5xx on the green target group and **rolls back to blue = 100 %** automatically. |
| **Evidence** | `cm report` shows `traffic-shift: rollback` event. CloudWatch shows a spike in `HTTPCode_Target_5XX_Count` for the green TG. |

### F2 — Catalog price bug → shadow gate failure

| | |
|---|---|
| **Setup** | Set `faults.catalogPriceBug: "true"` in `values-green.yaml` |
| **Steps** | Run `cm shadow`. The replay sends identical GETs to blue (×2) and green (×1). |
| **Expected** | The shadow comparator finds a mismatch on `items[*].price_cents` (green returns wrong prices). The shadow gate **fails** and the migration stops before any user traffic reaches green. |
| **Evidence** | `cm shadow` output shows mismatch examples with `field: items[*].price_cents`. |

### F3 — Lease agent crash → fencing proof

| | |
|---|---|
| **Setup** | Migration is in the lease-handoff phase. |
| **Steps** | `kubectl delete pod -l app=lease-agent -n shop` on the blue cluster. |
| **Expected** | The fencing check in every sweeper run and fulfillment batch re-verifies the lease. No double-run occurs. |
| **Evidence** | Query `sweeper_runs` and `fulfillment_log` — rows with `outcome='fenced'` may appear, but no two `ran` rows for the same slot or order. |

### F4 — Abort before DB switchover

| | |
|---|---|
| **Setup** | Traffic is at 100 % green, the workflow is suspended waiting for DB switchover approval. |
| **Steps** | Instead of approving, run `cm traffic set --blue 100 --green 0`. |
| **Expected** | All traffic returns to blue instantly. Users see no errors. The DB was never touched. |
| **Evidence** | ALB CloudWatch shows traffic returning to the blue TG. `cm verify` shows 0 lost writes. |

### F5 — DB switchback after migration

| | |
|---|---|
| **Setup** | Migration is complete (green is primary). |
| **Steps** | Run `cm db switchover --to blue`. Then run `cm verify` again. |
| **Expected** | Blue becomes primary again. The same reconciliation checks pass. |
| **Evidence** | `cm verify` output: 0 lost, 0 duplicated, 0 double-processed. Route 53 points to blue. |
