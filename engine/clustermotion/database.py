"""Zero-data-loss PostgreSQL switchover between clusters (CloudNativePG)."""
from __future__ import annotations

import time

import boto3
import psycopg
from kubernetes.client.exceptions import ApiException
from psycopg.conninfo import make_conninfo

from .checks import db_credentials, set_db_primary_annotation
from .config import COLORS, other
from .kube import KubeClients, wait_for

GROUP, VERSION, PLURAL = "postgresql.cnpg.io", "v1", "clusters"


def cnpg_name(cfg: dict, color: str) -> str:
    return f"{cfg['db']['cluster_prefix']}-{color}"


def get_cluster(cfg: dict, clients: KubeClients, color: str) -> dict:
    return clients.custom(color).get_namespaced_custom_object(
        GROUP, VERSION, cfg["db"]["namespace"], PLURAL, cnpg_name(cfg, color))


def patch_cluster(cfg: dict, clients: KubeClients, color: str, replica: dict) -> None:
    clients.custom(color).patch_namespaced_custom_object(
        GROUP, VERSION, cfg["db"]["namespace"], PLURAL, cnpg_name(cfg, color),
        {"spec": {"replica": replica}}, _content_type="application/merge-patch+json")


def lb_hostname(cfg: dict, clients: KubeClients, color: str) -> str:
    svc = clients.core(color).read_namespaced_service(cfg["db"]["lb_service"], cfg["db"]["namespace"])
    ingress = (svc.status.load_balancer.ingress or []) if svc.status.load_balancer else []
    if not ingress or not ingress[0].hostname:
        raise RuntimeError(f"{color}: {cfg['db']['lb_service']} has no load balancer hostname yet")
    return ingress[0].hostname


def in_recovery(cfg: dict, host: str) -> bool:
    creds = db_credentials(cfg)
    dsn = make_conninfo(host=host, dbname=cfg["db"]["name"], user=creds["username"],
                        password=creds["password"], connect_timeout=3)
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT pg_is_in_recovery()").fetchone()[0]


def dns_target(cfg: dict) -> str:
    r53 = boto3.client("route53")
    records = r53.list_resource_record_sets(HostedZoneId=cfg["db"]["zone_id"],
                                            StartRecordName=cfg["db"]["record"], StartRecordType="CNAME",
                                            MaxItems="1")["ResourceRecordSets"]
    if records and records[0]["Name"].rstrip(".") == cfg["db"]["record"]:
        return records[0]["ResourceRecords"][0]["Value"]
    return ""


def point_dns(cfg: dict, host: str) -> None:
    boto3.client("route53").change_resource_record_sets(
        HostedZoneId=cfg["db"]["zone_id"],
        ChangeBatch={"Comment": "clustermotion db switch", "Changes": [{
            "Action": "UPSERT",
            "ResourceRecordSet": {"Name": cfg["db"]["record"], "Type": "CNAME", "TTL": 5,
                                  "ResourceRecords": [{"Value": host}]},
        }]})


def status(cfg: dict, clients: KubeClients) -> dict:
    out = {"dns": dns_target(cfg)}
    for color in COLORS:
        try:
            obj = get_cluster(cfg, clients, color)
        except Exception as exc:
            out[color] = {"role": "absent", "healthy": False, "detail": type(exc).__name__}
            continue
        replica = obj["spec"].get("replica", {})
        st = obj.get("status", {})
        role = "primary" if replica.get("primary") == cnpg_name(cfg, color) else "replica"
        out[color] = {
            "role": role,
            "healthy": st.get("readyInstances") == obj["spec"].get("instances"),
            "phase": st.get("phase"),
            "primary_pod": st.get("currentPrimary"),
        }
    return out


def switchover(cfg: dict, clients: KubeClients, to: str, rec, timeout: int = 900) -> float:
    frm = other(to)
    src, dst = cnpg_name(cfg, frm), cnpg_name(cfg, to)
    state = status(cfg, clients)
    if state[frm]["role"] != "primary" or state[to]["role"] != "replica":
        raise RuntimeError(f"unexpected roles: {frm}={state[frm]['role']} {to}={state[to]['role']}")
    if not state[to]["healthy"]:
        raise RuntimeError(f"{dst} is not healthy: {state[to]}")
    target_host = lb_hostname(cfg, clients, to)
    rec.emit("db-switchover", "start", source=src, target=dst)

    t0 = time.time()
    patch_cluster(cfg, clients, frm, {"primary": dst})
    token = wait_for(lambda: get_cluster(cfg, clients, frm).get("status", {}).get("demotionToken"),
                     timeout, interval=2, desc=f"demotion token on {src}")
    rec.emit("db-switchover", "demoted", source=src, seconds=round(time.time() - t0, 1))

    patch_cluster(cfg, clients, to, {"primary": dst, "promotionToken": token})

    def promoted():
        try:
            return not in_recovery(cfg, target_host)
        except psycopg.OperationalError:
            return False

    wait_for(promoted, timeout, interval=2, desc=f"{dst} to accept writes")
    rec.emit("db-switchover", "promoted", target=dst, seconds=round(time.time() - t0, 1))

    point_dns(cfg, target_host)
    pause = round(time.time() - t0, 1)
    rec.emit("db-switchover", "completed", target=dst, write_pause_s=pause, dns=target_host)
    try:
        set_db_primary_annotation(cfg, clients, to)
    except ApiException as exc:
        print(f"warning: could not update GitOps annotation: {exc.reason}")
    return pause
