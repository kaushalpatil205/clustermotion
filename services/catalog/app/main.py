"""catalog-svc: stateless, read-only product catalog.

Role in ClusterMotion: the "easy" workload. It is migrated purely by shifting
traffic, and its GET endpoints are what the shadow-replay step compares
between blue and green. `generated_at` changes on every call on purpose: it
proves the Diffy-style noise filter works.
"""
import os
import random
import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram, make_asgi_app

CLUSTER = os.getenv("CLUSTER_NAME", "local")
# Fault-injection switches used by the failure tests (docs/07-testing.md).
FAULT_ERROR_RATE = float(os.getenv("FAULT_ERROR_RATE", "0"))
FAULT_PRICE_BUG = os.getenv("FAULT_PRICE_BUG", "false").lower() == "true"

PRODUCTS = {
    "sku-100": {"name": "Trail Running Shoes", "price_cents": 12999},
    "sku-101": {"name": "Merino Hiking Socks", "price_cents": 1999},
    "sku-102": {"name": "Waterproof Jacket", "price_cents": 18950},
    "sku-103": {"name": "Insulated Water Bottle", "price_cents": 3450},
    "sku-104": {"name": "Headlamp 400lm", "price_cents": 4225},
    "sku-105": {"name": "Trekking Poles", "price_cents": 8900},
    "sku-106": {"name": "Ultralight Tent", "price_cents": 34900},
    "sku-107": {"name": "Sleeping Bag -5C", "price_cents": 22900},
}

REQUESTS = Counter("catalog_requests_total", "HTTP requests", ["route", "status"])
LATENCY = Histogram("catalog_request_seconds", "Request latency", ["route"])

app = FastAPI(title="catalog-svc")
app.mount("/metrics", make_asgi_app())


def _route(path: str) -> str:
    return "/api/catalog/products/{sku}" if path.startswith("/api/catalog/products/") else path


@app.middleware("http")
async def observe(request: Request, call_next):
    route = _route(request.url.path)
    start = time.perf_counter()
    if FAULT_ERROR_RATE and not route.endswith("/healthz") and random.random() < FAULT_ERROR_RATE:
        response = JSONResponse({"error": "injected fault"}, status_code=500)
    else:
        response = await call_next(request)
    LATENCY.labels(route).observe(time.perf_counter() - start)
    REQUESTS.labels(route, str(response.status_code)).inc()
    response.headers["X-Served-By"] = CLUSTER  # header, not body: body must be comparable
    return response


def _product(sku: str) -> dict:
    item = PRODUCTS[sku]
    price = item["price_cents"] + (1 if FAULT_PRICE_BUG else 0)
    return {"sku": sku, "name": item["name"], "price_cents": price, "currency": "USD"}


@app.get("/api/catalog/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/catalog/products")
def list_products():
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": [_product(sku) for sku in sorted(PRODUCTS)],
    }


@app.get("/api/catalog/products/{sku}")
def get_product(sku: str):
    if sku not in PRODUCTS:
        raise HTTPException(status_code=404, detail="unknown sku")
    return _product(sku)
