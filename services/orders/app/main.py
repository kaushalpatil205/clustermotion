"""orders-svc: the stateful write path of the shop.

POST /api/orders   create an order (requires Idempotency-Key header)
GET  /api/orders/{order_id}
GET  /api/orders/healthz   liveness, used by the ALB target group
GET  /api/orders/readyz    checks the database is writable
"""
import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager

import boto3
import psycopg
from fastapi import FastAPI, Header, HTTPException, Response
from prometheus_client import Counter, make_asgi_app
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, Field

from app import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("orders")

CLUSTER = os.getenv("CLUSTER_NAME", "local")
QUEUE_URL = os.getenv("QUEUE_URL", "")

pool = db.make_pool()
schema_ready = threading.Event()
sqs = boto3.client("sqs") if QUEUE_URL else None

WRITES = Counter("orders_writes_total", "Order write attempts", ["result"])


def _init_schema() -> None:
    while not schema_ready.is_set():
        try:
            db.ensure_schema()
            schema_ready.set()
            log.info("schema ready")
        except Exception as exc:  # DB not up yet, or currently read-only
            log.warning("schema init failed (%s); retrying", type(exc).__name__)
            time.sleep(3)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    pool.open(wait=False)
    threading.Thread(target=_init_schema, daemon=True).start()
    yield
    pool.close()


app = FastAPI(title="orders-svc", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


@app.middleware("http")
async def served_by(request, call_next):
    response = await call_next(request)
    response.headers["X-Served-By"] = CLUSTER  # lets smoke tests prove which cluster answered
    return response


class OrderIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    qty: int = Field(gt=0, le=100)


def _unavailable(reason: str):
    WRITES.labels("unavailable").inc()
    raise HTTPException(status_code=503, detail=reason, headers={"Retry-After": "1"})


def _publish(order_id: str) -> None:
    if sqs is None:
        return
    try:
        sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps({"order_id": order_id}))
    except Exception as exc:  # the sweeper re-publishes PENDING orders later
        log.warning("publish failed for %s: %s", order_id, exc)


@app.get("/api/orders/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/orders/readyz")
def readyz():
    try:
        with pool.connection() as conn:
            in_recovery = conn.execute("SELECT pg_is_in_recovery()").fetchone()[0]
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"db unreachable: {type(exc).__name__}")
    if in_recovery:
        raise HTTPException(status_code=503, detail="db is read-only (replica)")
    return {"status": "ready", "schema": schema_ready.is_set(), "cluster": CLUSTER}


@app.post("/api/orders", status_code=201)
def create_order(
    body: OrderIn,
    response: Response,
    idempotency_key: str = Header(min_length=8, max_length=128),
):
    if not schema_ready.is_set():
        _unavailable("schema not ready")
    new_id = str(uuid.uuid4())
    try:
        with pool.connection() as conn:
            try:
                row = conn.execute(
                    """INSERT INTO orders (id, idempotency_key, sku, qty, created_by)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (idempotency_key) DO NOTHING
                       RETURNING id""",
                    (new_id, idempotency_key, body.sku, body.qty, CLUSTER),
                ).fetchone()
                created = row is not None
                if not created:
                    row = conn.execute(
                        "SELECT id FROM orders WHERE idempotency_key = %s", (idempotency_key,)
                    ).fetchone()
            except psycopg.errors.ReadOnlySqlTransaction:
                conn.close()  # connected to a demoted primary: drop it, reconnect via DNS
                raise
    except psycopg.errors.ReadOnlySqlTransaction:
        _unavailable("database primary is switching")
    except (psycopg.OperationalError, PoolTimeout) as exc:
        _unavailable(f"database unavailable: {type(exc).__name__}")

    order_id = str(row[0])
    if created:
        WRITES.labels("created").inc()
        _publish(order_id)
    else:
        WRITES.labels("replayed").inc()
        response.status_code = 200
    return {"order_id": order_id, "idempotent_replay": not created}


@app.get("/api/orders/{order_id}")
def get_order(order_id: uuid.UUID):
    try:
        with pool.connection() as conn:
            row = conn.execute(
                """SELECT id, sku, qty, status, created_by, fulfilled_by
                   FROM orders WHERE id = %s""",
                (order_id,),
            ).fetchone()
    except (psycopg.OperationalError, PoolTimeout) as exc:
        raise HTTPException(status_code=503, detail=type(exc).__name__, headers={"Retry-After": "1"})
    if row is None:
        raise HTTPException(status_code=404, detail="order not found")
    keys = ("order_id", "sku", "qty", "status", "created_by", "fulfilled_by")
    return {k: (str(v) if k == "order_id" else v) for k, v in zip(keys, row)}
