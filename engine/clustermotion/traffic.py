"""Progressive, SLO-gated traffic shifting with ALB weighted target groups."""
from __future__ import annotations

import datetime as dt
import time

import boto3

from .config import COLORS, other


def _elbv2(cfg):
    return boto3.client("elbv2", region_name=cfg["region"])


def current_weights(cfg: dict, service: str) -> dict:
    svc = cfg["services"][service]
    rule = _elbv2(cfg).describe_rules(RuleArns=[svc["rule_arn"]])["Rules"][0]
    forward = next(a for a in rule["Actions"] if a["Type"] == "forward")
    by_arn = {tg["TargetGroupArn"]: tg.get("Weight", 0) for tg in forward["ForwardConfig"]["TargetGroups"]}
    return {c: by_arn.get(svc["target_groups"][c]["arn"], 0) for c in COLORS}


def set_weights(cfg: dict, service: str, weights: dict) -> None:
    svc = cfg["services"][service]
    if sum(weights.values()) != 100:
        raise ValueError(f"weights must add up to 100: {weights}")
    _elbv2(cfg).modify_rule(RuleArn=svc["rule_arn"], Actions=[{
        "Type": "forward",
        "ForwardConfig": {
            "TargetGroups": [{"TargetGroupArn": svc["target_groups"][c]["arn"], "Weight": weights[c]}
                             for c in COLORS],
            "TargetGroupStickinessConfig": {"Enabled": False},
        },
    }])


def tg_stats(cfg: dict, service: str, color: str, start: dt.datetime, end: dt.datetime) -> dict:
    tg = cfg["services"][service]["target_groups"][color]["arn_suffix"]
    dims = [{"Name": "LoadBalancer", "Value": cfg["alb"]["arn_suffix"]},
            {"Name": "TargetGroup", "Value": tg}]

    def query(qid, metric, stat):
        return {"Id": qid, "MetricStat": {"Metric": {"Namespace": "AWS/ApplicationELB",
                                                     "MetricName": metric, "Dimensions": dims},
                                          "Period": 60, "Stat": stat}}

    resp = boto3.client("cloudwatch", region_name=cfg["region"]).get_metric_data(
        MetricDataQueries=[query("req", "RequestCount", "Sum"),
                           query("err", "HTTPCode_Target_5XX_Count", "Sum"),
                           query("p95", "TargetResponseTime", "p95")],
        StartTime=start, EndTime=end)
    values = {r["Id"]: r["Values"] for r in resp["MetricDataResults"]}
    return {"requests": sum(values.get("req", [])), "errors": sum(values.get("err", [])),
            "p95": max(values.get("p95", []), default=0.0)}


def evaluate(target: dict, source: dict, slo: dict) -> tuple[str, str]:
    """Return (verdict, reason); verdict is pass | fail | inconclusive."""
    if target["requests"] < slo["min_requests"]:
        return "inconclusive", f"only {target['requests']:.0f} requests observed"
    ratio = target["errors"] / target["requests"]
    if ratio > slo["max_5xx_ratio"]:
        return "fail", f"5xx ratio {ratio:.2%} > {slo['max_5xx_ratio']:.2%}"
    limit = max(slo["min_p95_seconds"], source["p95"] * slo["max_p95_ratio"])
    if target["p95"] > limit:
        return "fail", f"p95 {target['p95'] * 1000:.0f}ms > {limit * 1000:.0f}ms"
    return "pass", f"5xx {ratio:.2%}, p95 {target['p95'] * 1000:.0f}ms"


def shift(cfg: dict, service: str, to: str, steps: list[int], hold: int, rec,
          poll: int = 30, sleep=time.sleep) -> bool:
    frm = other(to)
    slo = cfg["slo"]
    for pct in steps:
        weights = {to: pct, frm: 100 - pct}
        set_weights(cfg, service, weights)
        rec.emit("shift", "step", service=service, weights=weights)
        step_start = dt.datetime.now(dt.timezone.utc)
        waited, verdict, reason = 0, "inconclusive", "no data yet"
        while waited < hold:
            sleep(min(poll, hold - waited))
            waited += min(poll, hold - waited)
            now = dt.datetime.now(dt.timezone.utc)
            window_start = step_start - dt.timedelta(minutes=1)  # CloudWatch has 1-minute buckets
            verdict, reason = evaluate(tg_stats(cfg, service, to, window_start, now),
                                       tg_stats(cfg, service, frm, window_start, now), slo)
            if verdict == "fail":
                set_weights(cfg, service, {frm: 100, to: 0})
                rec.emit("shift", "rolled-back", service=service, at_percent=pct, reason=reason)
                print(f"SLO breach on {service} at {pct}%: {reason}. Rolled back to {frm}=100.")
                return False
        rec.emit("shift", "step-passed", service=service, percent=pct, verdict=verdict, reason=reason)
        print(f"{service}: {to}={pct}% held {hold}s -> {verdict} ({reason})")
    rec.emit("shift", "completed", service=service, to=to)
    return True
