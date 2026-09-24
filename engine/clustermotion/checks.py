"""Cluster registration, readiness checks, preflight and smoke tests."""
from __future__ import annotations

import base64
import json
import time

import boto3
import requests
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from .config import COLORS, other
from .kube import KubeClients, wait_for

TARGET_HEADER = "X-CM-Target"


def db_credentials(cfg: dict) -> dict:
    sm = boto3.client("secretsmanager", region_name=cfg["region"])
    return json.loads(sm.get_secret_value(SecretId=cfg["db"]["secret_id"])["SecretString"])


def _apply_secret(core: client.CoreV1Api, namespace: str, secret: client.V1Secret) -> None:
    try:
        core.create_namespaced_secret(namespace, secret)
    except ApiException as exc:
        if exc.status != 409:
            raise
        core.replace_namespaced_secret(secret.metadata.name, namespace, secret)


def register(cfg: dict, clients: KubeClients, color: str, db_primary: str) -> None:
    """Add a workload cluster to Argo CD and seed its namespace + DB credentials."""
    name = cfg["clusters"][color]
    info = boto3.client("eks", region_name=cfg["region"]).describe_cluster(name=name)["cluster"]
    labels = {
        "argocd.argoproj.io/secret-type": "cluster",
        "clustermotion.io/workload": "true",
        "clustermotion.io/color": color,
    }
    annotations = dict(cfg["gitops"]["cluster_annotations"])
    annotations.update({
        "clustermotion.io/cluster-name": name,
        "clustermotion.io/db-primary": db_primary,
        "clustermotion.io/tg-catalog": cfg["services"]["catalog"]["target_groups"][color]["arn"],
        "clustermotion.io/tg-orders": cfg["services"]["orders"]["target_groups"][color]["arn"],
    })
    argocd_config = {
        "awsAuthConfig": {"clusterName": name},
        "tlsClientConfig": {"insecure": False, "caData": info["certificateAuthority"]["data"]},
    }
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(name=f"cluster-{name}", labels=labels, annotations=annotations),
        type="Opaque",
        string_data={"name": name, "server": info["endpoint"], "config": json.dumps(argocd_config)},
    )
    _apply_secret(clients.core("mgmt"), cfg["gitops"]["argocd_namespace"], secret)
    print(f"registered {name} in Argo CD (colour={color}, db-primary={db_primary})")

    # Namespace + DB credentials: the only secret not managed by GitOps.
    core, ns = clients.core(color), cfg["workload_namespace"]
    try:
        core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name=ns)))
    except ApiException as exc:
        if exc.status != 409:
            raise
    creds = db_credentials(cfg)
    _apply_secret(core, ns, client.V1Secret(
        metadata=client.V1ObjectMeta(name="orders-db-credentials"),
        type="kubernetes.io/basic-auth",
        string_data={"username": creds["username"], "password": creds["password"]},
    ))
    print(f"seeded namespace {ns} and orders-db-credentials in {name}")


def set_db_primary_annotation(cfg: dict, clients: KubeClients, primary: str) -> None:
    """Keep GitOps truthful after a switchover: charts render the new primary."""
    core = clients.core("mgmt")
    for color in COLORS:
        name = f"cluster-{cfg['clusters'][color]}"
        try:
            core.patch_namespaced_secret(name, cfg["gitops"]["argocd_namespace"],
                                         {"metadata": {"annotations": {"clustermotion.io/db-primary": primary}}})
        except ApiException as exc:
            if exc.status != 404:  # the other cluster may already be decommissioned
                raise


def argo_apps(cfg: dict, clients: KubeClients, color: str) -> list[dict]:
    apps = clients.custom("mgmt").list_namespaced_custom_object(
        "argoproj.io", "v1alpha1", cfg["gitops"]["argocd_namespace"], "applications")["items"]
    return [a for a in apps if a["metadata"]["name"].endswith(f"-{color}")]


def wait_synced(cfg: dict, clients: KubeClients, color: str, timeout: int = 1800) -> None:
    def all_green():
        apps = argo_apps(cfg, clients, color)
        pending = [a["metadata"]["name"] for a in apps
                   if a.get("status", {}).get("sync", {}).get("status") != "Synced"
                   or a.get("status", {}).get("health", {}).get("status") != "Healthy"]
        print(f"{len(apps) - len(pending)}/{len(apps)} apps Synced+Healthy; waiting for: {pending}", flush=True)
        return bool(apps) and not pending

    wait_for(all_green, timeout, interval=15, desc=f"Argo CD apps of {color}")


def http(cfg: dict, color: str, path: str, timeout: float = 5.0, **headers) -> requests.Response:
    url = f"http://{cfg['alb']['dns_name']}{path}"
    return requests.get(url, headers={TARGET_HEADER: color, **headers}, timeout=timeout)


def smoke(cfg: dict, color: str) -> list[tuple[str, bool, str]]:
    results = []
    for name, svc in cfg["services"].items():
        for path in [svc["health_path"], *svc.get("smoke_paths", [])]:
            try:
                resp = http(cfg, color, path)
                ok = resp.status_code == 200 and resp.headers.get("X-Served-By") == color
                detail = f"{resp.status_code} served-by={resp.headers.get('X-Served-By')}"
            except requests.RequestException as exc:
                ok, detail = False, type(exc).__name__
            results.append((f"{name} {path}", ok, detail))
    return results


def preflight(cfg: dict, clients: KubeClients, frm: str, to: str) -> list[tuple[str, bool, str]]:
    """Every check returns (ok, detail). A check that raises counts as failed."""
    from .database import status as db_status
    from .lease import LeaseTable
    from .traffic import current_weights

    elbv2 = boto3.client("elbv2", region_name=cfg["region"])

    def api_reachable(color):
        return True, clients.version(color)

    def target_apps_healthy():
        apps = argo_apps(cfg, clients, to)
        healthy = [a for a in apps if a.get("status", {}).get("health", {}).get("status") == "Healthy"]
        return bool(apps) and len(healthy) == len(apps), f"{len(healthy)}/{len(apps)} healthy"

    def targets_healthy(service):
        tg = cfg["services"][service]["target_groups"][to]["arn"]
        states = [d["TargetHealth"]["State"]
                  for d in elbv2.describe_target_health(TargetGroupArn=tg)["TargetHealthDescriptions"]]
        return states.count("healthy") > 0, f"{states.count('healthy')} healthy of {len(states)}"

    def weights_on_source(service):
        weights = current_weights(cfg, service)
        return weights[frm] == 100, weights

    def database_roles():
        s = db_status(cfg, clients)
        ok = s[frm]["role"] == "primary" and s[to]["role"] == "replica" and s[to]["healthy"]
        return ok, {c: s[c]["role"] for c in (frm, to)}

    def lease_on_source():
        holder = (LeaseTable.from_cfg(cfg).read() or {}).get("holder")
        return holder == frm, holder

    plan = [(f"{c} API reachable", api_reachable, (c,)) for c in (frm, to)]
    plan.append((f"{to} Argo CD apps healthy", target_apps_healthy, ()))
    for service in cfg["services"]:
        plan.append((f"{service}: {to} targets healthy", targets_healthy, (service,)))
        plan.append((f"{service}: weights {frm}=100", weights_on_source, (service,)))
    plan.append(("database roles", database_roles, ()))
    plan.append((f"lease held by {frm}", lease_on_source, ()))

    checks: list[tuple[str, bool, str]] = []
    for name, fn, fn_args in plan:
        try:
            ok, detail = fn(*fn_args)
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        checks.append((name, bool(ok), str(detail)))
    return checks


def print_checks(title: str, checks: list[tuple[str, bool, str]]) -> bool:
    print(f"\n{title}")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:40s} {detail}")
    passed = all(ok for _, ok, _ in checks)
    print(f"=> {'all checks passed' if passed else 'FAILED'}\n")
    return passed
