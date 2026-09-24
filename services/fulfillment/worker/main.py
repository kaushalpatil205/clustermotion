"""fulfillment-worker: consumes order events from SQS and fulfils orders."""
import json
import logging
import os
import signal
import time

import boto3
import psycopg
from psycopg.conninfo import make_conninfo

from shared.lease_guard import LeaseGuard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("fulfillment")

CLUSTER = os.getenv("CLUSTER_NAME", "local")
QUEUE_URL = os.getenv("QUEUE_URL", "")
WORK_SECONDS = float(os.getenv("WORK_SECONDS", "0.05"))  # simulated work per order

_stopping = False


def _request_stop(signum, _frame):
    global _stopping
    _stopping = True
    log.info("signal %s received: finishing in-flight messages, then exiting", signum)


def dsn() -> str:
    return make_conninfo(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "shop"),
        user=os.getenv("DB_USER", "shop"),
        password=os.getenv("DB_PASSWORD", "shop"),
        connect_timeout=3,
        application_name=f"fulfillment-{CLUSTER}",
    )


def parse_order_id(body: str) -> str | None:
    try:
        return str(json.loads(body)["order_id"])
    except (ValueError, KeyError, TypeError):
        return None


def fulfil(conn: psycopg.Connection, order_id: str) -> bool:
    """Idempotent: a duplicate delivery changes nothing (applied = False)."""
    with conn.transaction():
        row = conn.execute(
            """UPDATE orders SET status = 'FULFILLED', fulfilled_at = now(), fulfilled_by = %s
               WHERE id = %s AND status = 'PENDING' RETURNING id""",
            (CLUSTER, order_id),
        ).fetchone()
        applied = row is not None
        conn.execute(
            "INSERT INTO fulfillment_log (order_id, cluster, applied) VALUES (%s, %s, %s)",
            (order_id, CLUSTER, applied),
        )
    return applied


def main() -> None:
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    sqs = boto3.client("sqs")
    guard = LeaseGuard()
    conn = None
    log.info("worker started in cluster %s", CLUSTER)
    while not _stopping:
        if not guard.holds_lease():
            time.sleep(2)
            continue
        messages = sqs.receive_message(
            QueueUrl=QUEUE_URL, MaxNumberOfMessages=10, WaitTimeSeconds=5, VisibilityTimeout=60
        ).get("Messages", [])
        for msg in messages:
            order_id = parse_order_id(msg["Body"])
            if order_id is None:
                log.error("dropping malformed message: %r", msg["Body"][:200])
                sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
                continue
            try:
                if conn is None or conn.closed:
                    conn = psycopg.connect(dsn(), autocommit=True)
                time.sleep(WORK_SECONDS)
                applied = fulfil(conn, order_id)
                sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
                log.debug("order %s applied=%s", order_id, applied)
            except (psycopg.errors.ReadOnlySqlTransaction, psycopg.OperationalError) as exc:
                # Not deleted: SQS re-delivers it after the visibility timeout.
                log.warning("db unavailable (%s); will retry order %s", type(exc).__name__, order_id)
                if conn is not None:
                    conn.close()
                conn = None
                time.sleep(1)
    log.info("worker stopped cleanly")


if __name__ == "__main__":
    main()
