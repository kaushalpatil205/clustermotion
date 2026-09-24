"""Shadow replay: real GET requests from ALB access logs, compared Diffy-style."""
from __future__ import annotations

import datetime as dt
import gzip
import json
import random
import shlex
import urllib.parse
from dataclasses import dataclass, field

import boto3
import requests

from .checks import TARGET_HEADER

SHADOW_UA = "clustermotion-shadow/1.0"


def parse_alb_log_line(line: str) -> dict | None:
    """Parse one ALB access-log line; return method/path/status/user_agent."""
    try:
        parts = shlex.split(line)
    except ValueError:
        return None
    if len(parts) < 14:
        return None
    request = parts[12].split(" ")
    if len(request) != 3:
        return None
    method, url, _proto = request
    split = urllib.parse.urlsplit(url)
    path = split.path + (f"?{split.query}" if split.query else "")
    return {"time": parts[1], "method": method, "path": path,
            "elb_status": parts[8], "user_agent": parts[13]}


def select_requests(entries: list[dict], prefixes: list[str], limit: int, seed: int = 7) -> list[str]:
    paths = [e["path"] for e in entries
             if e and e["method"] == "GET" and e["user_agent"] != SHADOW_UA
             and e["elb_status"] in ("200", "404")
             and any(e["path"].startswith(p) for p in prefixes)
             and not e["path"].endswith(("/healthz", "/readyz"))]
    random.Random(seed).shuffle(paths)
    return paths[:limit]


def read_alb_logs(cfg: dict, minutes: int) -> list[dict]:
    s3 = boto3.client("s3", region_name=cfg["region"])
    account = boto3.client("sts", region_name=cfg["region"]).get_caller_identity()["Account"]
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(minutes=minutes)
    entries: list[dict] = []
    for day in {since.date(), now.date()}:
        prefix = (f"{cfg['alb']['logs_prefix']}/AWSLogs/{account}/elasticloadbalancing/"
                  f"{cfg['region']}/{day:%Y/%m/%d}/")
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=cfg["alb"]["logs_bucket"], Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["LastModified"] < since:
                    continue
                body = s3.get_object(Bucket=cfg["alb"]["logs_bucket"], Key=obj["Key"])["Body"].read()
                for line in gzip.decompress(body).decode().splitlines():
                    entries.append(parse_alb_log_line(line))
    return [e for e in entries if e]


def flatten(value, prefix: str = "") -> dict:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    if isinstance(value, list):
        out = {f"{prefix}.#len": len(value)}
        for i, v in enumerate(value):
            out.update(flatten(v, f"{prefix}[{i}]"))
        return out
    return {prefix: value}


@dataclass
class Response:
    status: int
    body: object


@dataclass
class Verdict:
    outcome: str                       # match | mismatch | noisy
    fields: list[str] = field(default_factory=list)


def compare(primary: Response, secondary: Response, candidate: Response) -> Verdict:
    if primary.status != secondary.status:
        return Verdict("noisy", ["status"])
    if candidate.status != primary.status:
        return Verdict("mismatch", [f"status {primary.status}!={candidate.status}"])
    fp, fs, fc = flatten(primary.body), flatten(secondary.body), flatten(candidate.body)
    noise = {k for k in fp.keys() | fs.keys() if fp.get(k) != fs.get(k)}
    diffs = sorted(k for k in (fp.keys() | fc.keys()) - noise if fp.get(k) != fc.get(k))
    return Verdict("mismatch", diffs[:5]) if diffs else Verdict("match")


def _fetch(cfg: dict, color: str, path: str) -> Response:
    resp = requests.get(f"http://{cfg['alb']['dns_name']}{path}", timeout=5,
                        headers={TARGET_HEADER: color, "User-Agent": SHADOW_UA})
    try:
        body = resp.json()
    except json.JSONDecodeError:
        body = resp.text
    return Response(resp.status_code, body)


def run(cfg: dict, frm: str, to: str, minutes: int, max_requests: int,
        paths: list[str] | None = None) -> dict:
    if paths is None:
        prefixes = [p for svc in cfg["services"].values() for p in svc.get("shadow_prefixes", [])]
        paths = select_requests(read_alb_logs(cfg, minutes), prefixes, max_requests)
    counts = {"match": 0, "mismatch": 0, "noisy": 0, "error": 0}
    examples: list[dict] = []
    for path in paths[:max_requests]:
        try:
            verdict = compare(_fetch(cfg, frm, path), _fetch(cfg, frm, path), _fetch(cfg, to, path))
        except requests.RequestException:
            counts["error"] += 1
            continue
        counts[verdict.outcome] += 1
        if verdict.outcome == "mismatch" and len(examples) < 10:
            examples.append({"path": path, "fields": verdict.fields})
    judged = counts["match"] + counts["mismatch"]
    return {"requests": len(paths), **counts,
            "mismatch_ratio": round(counts["mismatch"] / judged, 4) if judged else None,
            "examples": examples}
