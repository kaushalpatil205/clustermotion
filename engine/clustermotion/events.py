"""Run timeline: structured events + Markdown report."""
from __future__ import annotations

import datetime as dt
import json
import os
import time

import boto3


class Recorder:
    def __init__(self, cfg: dict, run_id: str | None = None):
        self.run_id = run_id or os.getenv("CM_RUN_ID") or time.strftime("manual-%Y%m%d-%H%M%S")
        self.table = cfg.get("runs_table")
        self._ddb = boto3.client("dynamodb", region_name=cfg["region"]) if self.table else None

    def emit(self, step: str, status: str, **data) -> None:
        ts = time.time()
        print(json.dumps({"run_id": self.run_id, "ts": round(ts, 3), "step": step,
                          "status": status, **data}, default=str), flush=True)
        if not self._ddb:
            return
        try:
            self._ddb.put_item(TableName=self.table, Item={
                "run_id": {"S": self.run_id},
                "ts": {"N": f"{ts:.6f}"},
                "step": {"S": step},
                "status": {"S": status},
                "data": {"S": json.dumps(data, default=str)},
            })
        except Exception as exc:  # never fail a migration because of telemetry
            print(f"warning: could not record event: {exc}", flush=True)

    def events(self) -> list[dict]:
        if not self._ddb:
            return []
        out, kwargs = [], {
            "TableName": self.table,
            "KeyConditionExpression": "run_id = :r",
            "ExpressionAttributeValues": {":r": {"S": self.run_id}},
        }
        while True:
            page = self._ddb.query(**kwargs)
            for item in page["Items"]:
                out.append({
                    "ts": float(item["ts"]["N"]),
                    "step": item["step"]["S"],
                    "status": item["status"]["S"],
                    "data": json.loads(item["data"]["S"]),
                })
            if "LastEvaluatedKey" not in page:
                return sorted(out, key=lambda e: e["ts"])
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def render_markdown(run_id: str, events: list[dict], final_status: str = "") -> str:
    if not events:
        return f"# ClusterMotion run `{run_id}`\n\nNo events recorded.\n"
    start = events[0]["ts"]
    lines = [
        f"# ClusterMotion run `{run_id}`",
        "",
        f"- Started: {dt.datetime.fromtimestamp(start, dt.timezone.utc):%Y-%m-%d %H:%M:%S} UTC",
        f"- Duration: {(events[-1]['ts'] - start) / 60:.1f} min",
        f"- Final status: **{final_status or events[-1]['status']}**",
        "",
        "| t+ (s) | step | status | details |",
        "|---:|---|---|---|",
    ]
    for e in events:
        details = ", ".join(f"{k}={v}" for k, v in e["data"].items())
        lines.append(f"| {e['ts'] - start:.0f} | {e['step']} | {e['status']} | {details[:160]} |")
    return "\n".join(lines) + "\n"
