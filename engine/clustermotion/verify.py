"""Reconciliation: prove nothing was lost, duplicated or processed twice."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

from .checks import db_credentials


def load_confirmed(path: str) -> list[dict]:
    rows = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line.startswith("{"):
            rec = json.loads(line)
            if rec.get("event") == "confirmed":
                rows.append(rec)
    return rows


def max_gap(timestamps_ms: list[int]) -> tuple[float, int | None]:
    """Longest interval (seconds) between consecutive confirmed writes, and when it started."""
    ts = sorted(timestamps_ms)
    best, at = 0.0, None
    for a, b in zip(ts, ts[1:]):
        if (b - a) / 1000 > best:
            best, at = (b - a) / 1000, a
    return best, at


QUERIES = {
    "lost_writes": "SELECT count(*) FROM confirmed c LEFT JOIN orders o ON o.id = c.order_id WHERE o.id IS NULL",
    "id_mismatch": """SELECT count(*) FROM confirmed c JOIN orders o ON o.idempotency_key = c.idem_key
                      WHERE o.id <> c.order_id""",
    "duplicate_keys": """SELECT count(*) FROM (SELECT idempotency_key FROM orders
                         GROUP BY 1 HAVING count(*) > 1) d""",
    "double_fulfilled": """SELECT count(*) FROM (SELECT order_id FROM fulfillment_log WHERE applied
                           GROUP BY 1 HAVING count(*) > 1) d""",
    "unfulfilled": """SELECT count(*) FROM confirmed c JOIN orders o ON o.id = c.order_id
                      WHERE o.status <> 'FULFILLED'""",
    "missed_slots": """SELECT count(*) FROM generate_series(
                           date_bin('%(slot)s seconds', %%(since)s::timestamptz, 'epoch'),
                           date_bin('%(slot)s seconds', %%(until)s::timestamptz, 'epoch'),
                           make_interval(secs => %(slot)s)) s(slot)
                       WHERE NOT EXISTS (SELECT 1 FROM sweeper_runs r
                                         WHERE r.slot = s.slot AND r.outcome = 'ran')""",
    "duplicate_slot_runs": """SELECT count(*) FROM (SELECT slot FROM sweeper_runs WHERE outcome = 'ran'
                              AND slot >= %(since)s GROUP BY 1 HAVING count(*) > 1) d""",
}

INFO_QUERIES = {
    "created_by": "SELECT o.created_by, count(*) FROM confirmed c JOIN orders o ON o.id = c.order_id GROUP BY 1",
    "fulfilled_by": "SELECT o.fulfilled_by, count(*) FROM confirmed c JOIN orders o ON o.id = c.order_id GROUP BY 1",
    "sweeper_outcomes": """SELECT cluster || ':' || outcome, count(*) FROM sweeper_runs
                           WHERE slot >= %(since)s GROUP BY 1""",
    "consumer_windows": """SELECT cluster, min(processed_at), max(processed_at) FROM fulfillment_log
                           WHERE processed_at >= %(since)s AND applied GROUP BY 1""",
}


def overlap_seconds(windows: list[tuple]) -> float:
    if len(windows) < 2:
        return 0.0
    (_, s1, e1), (_, s2, e2) = windows[:2]
    return max(0.0, (min(e1, e2) - max(s1, s2)).total_seconds())


def reconcile(cfg: dict, confirmed_path: str, host: str | None = None,
              slot_seconds: int = 120, grace_slots: int = 1) -> dict:
    confirmed = load_confirmed(confirmed_path)
    if not confirmed:
        raise SystemExit(f"no confirmed writes found in {confirmed_path}")
    since = dt.datetime.fromtimestamp(min(r["t"] for r in confirmed) / 1000, dt.timezone.utc)
    until = dt.datetime.fromtimestamp(max(r["t"] for r in confirmed) / 1000, dt.timezone.utc)
    # Ignore the first and last slot: the load test may start/stop mid-slot.
    since_slots = since + dt.timedelta(seconds=slot_seconds * grace_slots)
    until_slots = until - dt.timedelta(seconds=slot_seconds * grace_slots)
    creds = db_credentials(cfg)
    dsn = make_conninfo(host=host or cfg["db"]["record"], dbname=cfg["db"]["name"],
                        user=creds["username"], password=creds["password"], connect_timeout=5)
    params = {"since": since_slots, "until": until_slots}
    result: dict = {"confirmed_writes": len(confirmed), "window": [since.isoformat(), until.isoformat()]}
    with psycopg.connect(dsn) as conn:
        conn.execute("CREATE TEMP TABLE confirmed (order_id uuid, idem_key text) ON COMMIT DROP")
        with conn.cursor().copy("COPY confirmed (order_id, idem_key) FROM STDIN") as copy:
            for r in confirmed:
                copy.write_row((r["order_id"], r["key"]))
        for name, sql in QUERIES.items():
            sql = sql % {"slot": slot_seconds} if "%(slot)s" in sql else sql
            result[name] = conn.execute(sql, params if "%(" in sql else None).fetchone()[0]
        for name, sql in INFO_QUERIES.items():
            rows = conn.execute(sql, params if "%(" in sql else None).fetchall()
            if name == "consumer_windows":
                result["consumer_overlap_s"] = overlap_seconds(rows)
                result[name] = {r[0]: [r[1].isoformat(), r[2].isoformat()] for r in rows}
            else:
                result[name] = {str(k): v for k, v in rows}
    gap, gap_at = max_gap([r["t"] for r in confirmed])
    result["max_write_gap_s"] = gap
    result["max_write_gap_at"] = (dt.datetime.fromtimestamp(gap_at / 1000, dt.timezone.utc).isoformat()
                                  if gap_at else None)
    must_be_zero = ["lost_writes", "id_mismatch", "duplicate_keys", "double_fulfilled",
                    "unfulfilled", "missed_slots", "duplicate_slot_runs"]
    result["passed"] = all(result[k] == 0 for k in must_be_zero) and result["consumer_overlap_s"] == 0
    return result


def render(result: dict) -> str:
    lines = ["## Reconciliation", "", "| check | value | expected |", "|---|---:|---:|"]
    for key in ["confirmed_writes", "lost_writes", "id_mismatch", "duplicate_keys", "double_fulfilled",
                "unfulfilled", "missed_slots", "duplicate_slot_runs", "consumer_overlap_s", "max_write_gap_s"]:
        expected = "-" if key in ("confirmed_writes", "max_write_gap_s") else "0"
        lines.append(f"| {key} | {result[key]} | {expected} |")
    lines += ["", f"- Orders created by: `{result['created_by']}`",
              f"- Orders fulfilled by: `{result['fulfilled_by']}`",
              f"- Sweeper attempts: `{result['sweeper_outcomes']}`",
              f"- Consumer windows: `{result['consumer_windows']}`",
              "", f"**Result: {'PASSED' if result['passed'] else 'FAILED'}**", ""]
    return "\n".join(lines)
