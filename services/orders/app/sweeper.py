"""order-sweeper: CronJob entrypoint (a singleton workload).

1. Fencing: record 'fenced' and exit unless this cluster holds the lease.
2. Idempotent claim: insert the 'ran' row for this schedule slot. A unique
   index allows only one per slot; a second attempt records
   'duplicate-skipped' and exits.
3. Re-publish orders stuck in PENDING (their SQS publish failed).
"""
import json
import logging
import os
import sys
import time

import boto3
import psycopg

from app import db
from shared.lease_guard import LeaseGuard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sweeper")

CLUSTER = os.getenv("CLUSTER_NAME", "local")
QUEUE_URL = os.getenv("QUEUE_URL", "")
SLOT_SECONDS = int(os.getenv("SWEEPER_SLOT_SECONDS", "120"))
STALE_SECONDS = int(os.getenv("SWEEPER_STALE_SECONDS", "60"))

CLAIM_SQL = """
INSERT INTO sweeper_runs (slot, cluster, outcome) VALUES (to_timestamp(%s), %s, 'ran')
ON CONFLICT (slot) WHERE outcome = 'ran' DO NOTHING
RETURNING id
"""


def current_slot(now: float | None = None) -> int:
    now = time.time() if now is None else now
    return int(now // SLOT_SECONDS * SLOT_SECONDS)


def record(conn: psycopg.Connection, slot: int, outcome: str) -> None:
    conn.execute(
        "INSERT INTO sweeper_runs (slot, cluster, outcome) VALUES (to_timestamp(%s), %s, %s)",
        (slot, CLUSTER, outcome),
    )


def main() -> int:
    slot = current_slot()
    holds_lease = LeaseGuard().holds_lease()
    with psycopg.connect(db.dsn()) as conn:
        if not holds_lease:
            record(conn, slot, "fenced")
            log.info("slot %s: cluster %s does not hold the lease; fenced", slot, CLUSTER)
            return 0
        claim = conn.execute(CLAIM_SQL, (slot, CLUSTER)).fetchone()
        if claim is None:
            record(conn, slot, "duplicate-skipped")
            log.info("slot %s already executed; duplicate attempt skipped", slot)
            return 0
        stuck = conn.execute(
            """SELECT id FROM orders
               WHERE status = 'PENDING' AND created_at < now() - make_interval(secs => %s)
               ORDER BY created_at LIMIT 500""",
            (STALE_SECONDS,),
        ).fetchall()
        sqs = boto3.client("sqs") if QUEUE_URL else None
        for (order_id,) in stuck:
            if sqs:
                sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps({"order_id": str(order_id)}))
        conn.execute("UPDATE sweeper_runs SET republished = %s WHERE id = %s", (len(stuck), claim[0]))
    log.info("slot %s: re-published %d stuck orders", slot, len(stuck))
    return 0


if __name__ == "__main__":
    sys.exit(main())
