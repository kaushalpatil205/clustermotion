"""Singleton lease: exactly one cluster runs CronJobs and queue consumers."""
from __future__ import annotations

import logging
import os
import signal
import time

import boto3
from kubernetes import client
from kubernetes import config as kconfig

log = logging.getLogger("lease")

SELECTOR = "clustermotion.io/class=singleton"
PAUSE = "autoscaling.keda.sh/paused-replicas"
KEDA = ("keda.sh", "v1alpha1", "scaledobjects")


def decide(state: dict | None, me: str, now: float, ttl: float, drained: bool) -> str:
    """Pure decision function: on | off | drain | release | acquire."""
    if not state:
        return "off"
    holder, desired = state.get("holder", ""), state.get("desired", "")
    if holder == me:
        if desired == me:
            return "on"
        return "release" if drained else "drain"
    expired = now - float(state.get("renewed_at", 0)) > ttl
    if desired == me and (holder == "" or expired):
        return "acquire"
    return "off"


class LeaseTable:
    def __init__(self, table: str, lease_id: str, region: str | None = None, client_=None):
        self.table, self.lease_id = table, lease_id
        self.ddb = client_ or boto3.client("dynamodb", region_name=region)

    @classmethod
    def from_cfg(cls, cfg: dict) -> "LeaseTable":
        return cls(cfg["lease"]["table"], cfg["lease"]["lease_id"], cfg["region"])

    @property
    def key(self):
        return {"lease_id": {"S": self.lease_id}}

    def read(self) -> dict | None:
        item = self.ddb.get_item(TableName=self.table, Key=self.key, ConsistentRead=True).get("Item")
        if not item:
            return None
        return {"holder": item.get("holder", {}).get("S", ""),
                "desired": item.get("desired", {}).get("S", ""),
                "renewed_at": float(item.get("renewed_at", {}).get("N", "0"))}

    def init(self, holder: str) -> None:
        self.ddb.put_item(TableName=self.table, Item={
            **self.key, "holder": {"S": holder}, "desired": {"S": holder},
            "renewed_at": {"N": str(int(time.time()))}})

    def set_desired(self, color: str) -> None:
        self.ddb.update_item(TableName=self.table, Key=self.key,
                             UpdateExpression="SET desired = :d",
                             ExpressionAttributeValues={":d": {"S": color}})

    def _conditional(self, update: str, condition: str, values: dict) -> bool:
        try:
            self.ddb.update_item(TableName=self.table, Key=self.key, UpdateExpression=update,
                                 ConditionExpression=condition, ExpressionAttributeValues=values)
            return True
        except self.ddb.exceptions.ConditionalCheckFailedException:
            return False

    def renew(self, me: str) -> bool:
        return self._conditional("SET renewed_at = :now", "holder = :me",
                                 {":now": {"N": str(int(time.time()))}, ":me": {"S": me}})

    def release(self, me: str) -> bool:
        return self._conditional("SET holder = :empty", "holder = :me",
                                 {":empty": {"S": ""}, ":me": {"S": me}})

    def acquire(self, me: str, seen: dict) -> bool:
        """Optimistic: succeed only if nobody changed the lease since we read it."""
        return self._conditional(
            "SET holder = :me, renewed_at = :now",
            "holder = :seen_holder AND renewed_at = :seen_renewed",
            {":me": {"S": me}, ":now": {"N": str(int(time.time()))},
             ":seen_holder": {"S": seen["holder"]}, ":seen_renewed": {"N": str(int(seen["renewed_at"]))}})


class Agent:
    """Runs inside each workload cluster; switches singleton workloads on/off."""

    def __init__(self, table: LeaseTable, me: str, namespace: str, ttl: float = 30, interval: float = 5):
        self.table, self.me, self.ns, self.ttl, self.interval = table, me, namespace, ttl, interval
        self.batch = client.BatchV1Api()
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.custom = client.CustomObjectsApi()
        self._stop = False

    def ensure(self, on: bool) -> None:
        for cj in self.batch.list_namespaced_cron_job(self.ns, label_selector=SELECTOR).items:
            if bool(cj.spec.suspend) == on:  # needs a change
                self.batch.patch_namespaced_cron_job(cj.metadata.name, self.ns, {"spec": {"suspend": not on}})
                log.info("cronjob %s suspend=%s", cj.metadata.name, not on)
        for so in self.custom.list_namespaced_custom_object(*KEDA[:2], self.ns, KEDA[2],
                                                           label_selector=SELECTOR)["items"]:
            paused = PAUSE in (so["metadata"].get("annotations") or {})
            if paused == on:  # needs a change
                self.custom.patch_namespaced_custom_object(
                    *KEDA[:2], self.ns, KEDA[2], so["metadata"]["name"],
                    {"metadata": {"annotations": {PAUSE: None if on else "0"}}},
                    _content_type="application/merge-patch+json")
                log.info("scaledobject %s paused=%s", so["metadata"]["name"], not on)

    def drained(self) -> bool:
        jobs = self.batch.list_namespaced_job(self.ns, label_selector=SELECTOR).items
        if any((j.status.active or 0) > 0 for j in jobs):
            return False
        for so in self.custom.list_namespaced_custom_object(*KEDA[:2], self.ns, KEDA[2],
                                                           label_selector=SELECTOR)["items"]:
            target = so["spec"]["scaleTargetRef"]["name"]
            dep = self.apps.read_namespaced_deployment(target, self.ns)
            selector = ",".join(f"{k}={v}" for k, v in dep.spec.selector.match_labels.items())
            if self.core.list_namespaced_pod(self.ns, label_selector=selector).items:
                return False  # includes pods still terminating gracefully
        return True

    def tick(self) -> str:
        state = self.table.read()
        holder_is_me = bool(state) and state.get("holder") == self.me
        action = decide(state, self.me, time.time(), self.ttl,
                        drained=holder_is_me and state.get("desired") != self.me and self._drain())
        if action == "on":
            if self.table.renew(self.me):
                self.ensure(True)
            else:
                self.ensure(False)
        elif action == "release":
            if self.table.release(self.me):
                log.info("released lease (desired=%s)", state.get("desired"))
        elif action == "acquire":
            self.ensure(False)
            if self.table.acquire(self.me, state):
                log.info("acquired lease from %r", state.get("holder"))
        elif action in ("off", "drain"):
            self.ensure(False)
        return action

    def _drain(self) -> bool:
        self.ensure(False)
        return self.drained()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        last = None
        while not self._stop:
            try:
                action = self.tick()
                if action != last:
                    log.info("lease action: %s", action)
                    last = action
            except Exception as exc:
                log.exception("tick failed: %s", exc)
            time.sleep(self.interval)


def run_agent() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    kconfig.load_incluster_config()
    table = LeaseTable(os.environ["LEASE_TABLE"], os.getenv("LEASE_ID", "singletons"))
    Agent(table, os.environ["CLUSTER_NAME"], os.environ["WATCH_NAMESPACE"],
          ttl=float(os.getenv("LEASE_TTL_SECONDS", "30"))).run()


def handoff(cfg: dict, to: str, rec, timeout: int = 900) -> float:
    table = LeaseTable.from_cfg(cfg)
    t0 = time.time()
    table.set_desired(to)
    rec.emit("lease-handoff", "requested", to=to)
    deadline = t0 + timeout
    while time.time() < deadline:
        state = table.read() or {}
        if state.get("holder") == to:
            took = round(time.time() - t0, 1)
            rec.emit("lease-handoff", "completed", holder=to, seconds=took)
            return took
        time.sleep(3)
    raise TimeoutError(f"lease not acquired by {to} within {timeout}s (state={table.read()})")
