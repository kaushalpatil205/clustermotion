"""Migration planner: classify every workload into a migration strategy."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

SINGLETON = ("clustermotion.io/class", "singleton")
PART_OF = ("app.kubernetes.io/part-of", "clustermotion")
ORDER = {"platform": 0, "redeploy": 1, "traffic-shift": 2, "db-switchover": 3,
         "lease-handoff": 4, "snapshot-restore": 5, "skip": 9}
PLATFORM_NAMESPACES = {"kube-system", "cert-manager", "cnpg-system", "keda"}
CUSTOM_KINDS = [  # (group, version, plural, kind)
    ("elbv2.k8s.aws", "v1beta1", "targetgroupbindings", "TargetGroupBinding"),
    ("keda.sh", "v1alpha1", "scaledobjects", "ScaledObject"),
    ("postgresql.cnpg.io", "v1", "clusters", "Cluster"),
]


@dataclass
class PlanItem:
    kind: str
    namespace: str
    name: str
    strategy: str
    reason: str
    est_downtime_s: int = 0
    needs_approval: bool = False


def _labels(obj: dict) -> dict:
    return obj.get("metadata", {}).get("labels") or {}


def _has(labels: dict, pair: tuple[str, str]) -> bool:
    return labels.get(pair[0]) == pair[1]


def _gib(quantity: str | None) -> float:
    if not quantity:
        return 10.0  # unknown size: assume 10 GiB
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti)?", str(quantity))
    if not m:
        return 10.0
    factor = {"Ki": 1 / 1024**2, "Mi": 1 / 1024, "Gi": 1, "Ti": 1024, None: 1 / 1024**3}[m.group(2)]
    return float(m.group(1)) * factor


def _selector_matches(selector: dict, labels: dict) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def classify(objects: list[dict]) -> list[PlanItem]:
    def key(o):
        return o.get("metadata", {}).get("namespace", "default"), o["metadata"]["name"]

    services = {key(o): o for o in objects if o.get("kind") == "Service"}
    pvcs = {key(o): o for o in objects if o.get("kind") == "PersistentVolumeClaim"}
    bound_services = {(key(o)[0], o["spec"]["serviceRef"]["name"])
                      for o in objects if o.get("kind") == "TargetGroupBinding"}
    scaled = {(key(o)[0], o["spec"]["scaleTargetRef"]["name"]): o
              for o in objects if o.get("kind") == "ScaledObject"}

    items: list[PlanItem] = []
    for obj in objects:
        kind = obj.get("kind", "")
        ns, name = key(obj) if "metadata" in obj else ("", "")
        labels = _labels(obj)
        api = obj.get("apiVersion", "")

        if _has(labels, PART_OF):
            items.append(PlanItem(kind, ns, name, "skip", "ClusterMotion component"))
        elif kind == "CustomResourceDefinition" or (ns in PLATFORM_NAMESPACES and kind in ("Deployment", "DaemonSet")):
            items.append(PlanItem(kind, ns, name, "platform", "installed on the target by GitOps"))
        elif kind == "Cluster" and api.startswith("postgresql.cnpg.io"):
            items.append(PlanItem(kind, ns, name, "db-switchover",
                                  "CloudNativePG replica catches up, then demote/promote with token",
                                  est_downtime_s=30))
        elif kind == "CronJob":
            labelled = _has(labels, SINGLETON)
            items.append(PlanItem(
                kind, ns, name, "lease-handoff",
                "CronJobs fire once per cluster; must run in exactly one" +
                ("" if labelled else " (add label clustermotion.io/class=singleton)"),
                needs_approval=not labelled))
        elif kind in ("Deployment", "StatefulSet"):
            items.append(_classify_pod_owner(obj, ns, name, labels, services, pvcs, bound_services, scaled))
    return sorted(items, key=lambda i: (ORDER[i.strategy], i.namespace, i.name))


def _classify_pod_owner(obj, ns, name, labels, services, pvcs, bound_services, scaled) -> PlanItem:
    kind = obj["kind"]
    spec = obj.get("spec", {})
    pod_labels = spec.get("template", {}).get("metadata", {}).get("labels") or {}
    volumes = spec.get("template", {}).get("spec", {}).get("volumes") or []
    claim_names = [v["persistentVolumeClaim"]["claimName"] for v in volumes if v.get("persistentVolumeClaim")]
    sizes = [_gib(pvcs.get((ns, c), {}).get("spec", {}).get("resources", {}).get("requests", {}).get("storage"))
             for c in claim_names]
    sizes += [_gib(t.get("spec", {}).get("resources", {}).get("requests", {}).get("storage"))
              for t in spec.get("volumeClaimTemplates") or []]

    scaled_object = scaled.get((ns, name))
    if _has(labels, SINGLETON) or (scaled_object and _has(_labels(scaled_object), SINGLETON)):
        return PlanItem(kind, ns, name, "lease-handoff", "singleton consumer; paused outside the lease holder")
    if sizes:
        gib = sum(sizes)
        return PlanItem(kind, ns, name, "snapshot-restore",
                        f"{gib:.0f} GiB on PVCs without replication; volume must be copied",
                        est_downtime_s=int(60 + 20 * gib), needs_approval=True)
    exposing = [svc_name for (svc_ns, svc_name), svc in services.items()
                if svc_ns == ns and _selector_matches(svc.get("spec", {}).get("selector") or {}, pod_labels)]
    if any((ns, s) in bound_services for s in exposing):
        return PlanItem(kind, ns, name, "traffic-shift", "behind ALB TargetGroupBinding; weights move blue->green")
    if exposing:
        return PlanItem(kind, ns, name, "traffic-shift",
                        f"has Service {exposing[0]} but no TargetGroupBinding; add one to shift traffic",
                        needs_approval=True)
    return PlanItem(kind, ns, name, "redeploy", "stateless, no inbound traffic")


def objects_from_manifests(path: str) -> list[dict]:
    files = [Path(path)] if Path(path).is_file() else sorted(Path(path).rglob("*.y*ml"))
    return [doc for f in files for doc in yaml.safe_load_all(f.read_text()) if isinstance(doc, dict) and doc.get("kind")]


def objects_from_cluster(clients, color: str, namespaces: list[str]) -> list[dict]:
    from kubernetes.client.exceptions import ApiException

    api = clients.api(color)
    apps, batch, core, custom = clients.apps(color), clients.batch(color), clients.core(color), clients.custom(color)
    listers = [
        ("apps/v1", "Deployment", apps.list_namespaced_deployment),
        ("apps/v1", "StatefulSet", apps.list_namespaced_stateful_set),
        ("batch/v1", "CronJob", batch.list_namespaced_cron_job),
        ("v1", "Service", core.list_namespaced_service),
        ("v1", "PersistentVolumeClaim", core.list_namespaced_persistent_volume_claim),
    ]
    out: list[dict] = []
    for ns in namespaces:
        for api_version, kind, lister in listers:
            for item in lister(ns).items:
                d = api.sanitize_for_serialization(item)
                d.update(apiVersion=api_version, kind=kind)
                out.append(d)
        for group, version, plural, kind in CUSTOM_KINDS:
            try:
                resp = custom.list_namespaced_custom_object(group, version, ns, plural)
            except ApiException as exc:
                if exc.status == 404:  # CRD not installed in this cluster
                    continue
                raise
            for item in resp.get("items", []):
                item.update(apiVersion=f"{group}/{version}", kind=kind)
                out.append(item)
    return out


def render(items: list[PlanItem], as_json: bool = False) -> str:
    if as_json:
        return json.dumps([asdict(i) for i in items], indent=2)
    rows = [("#", "KIND", "NAMESPACE/NAME", "STRATEGY", "EST.DOWNTIME", "APPROVAL", "REASON")]
    for n, i in enumerate(items, 1):
        rows.append((str(n), i.kind, f"{i.namespace}/{i.name}", i.strategy,
                     f"{i.est_downtime_s}s" if i.est_downtime_s else "-",
                     "REQUIRED" if i.needs_approval else "-", i.reason))
    widths = [max(len(r[c]) for r in rows) for c in range(len(rows[0]) - 1)]
    lines = ["  ".join(r[c].ljust(widths[c]) for c in range(len(widths))) + "  " + r[-1] for r in rows]
    counts: dict[str, int] = {}
    for i in items:
        counts[i.strategy] = counts.get(i.strategy, 0) + 1
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda kv: ORDER[kv[0]]))
    approvals = sum(i.needs_approval for i in items)
    lines += ["", f"Plan: {len(items)} workloads -> {summary}; {approvals} need approval"]
    return "\n".join(lines)
