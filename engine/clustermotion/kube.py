"""Kubernetes API clients for blue/green (EKS) and the management cluster."""
from __future__ import annotations

import base64
import os
import tempfile
import time

import boto3
from botocore.signers import RequestSigner
from kubernetes import client
from kubernetes import config as kconfig


def eks_token(cluster_name: str, region: str) -> str:
    session = boto3.session.Session(region_name=region)
    sts = session.client("sts")
    signer = RequestSigner(
        sts.meta.service_model.service_id, region, "sts", "v4",
        session.get_credentials(), session.events,
    )
    url = signer.generate_presigned_url(
        {
            "method": "GET",
            "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
            "body": {},
            "headers": {"x-k8s-aws-id": cluster_name},
            "context": {},
        },
        region_name=region, expires_in=60, operation_name="",
    )
    return "k8s-aws-v1." + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def eks_api_client(cluster_name: str, region: str) -> client.ApiClient:
    info = boto3.client("eks", region_name=region).describe_cluster(name=cluster_name)["cluster"]
    ca = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
    ca.write(base64.b64decode(info["certificateAuthority"]["data"]))
    ca.close()
    cfg = client.Configuration()
    cfg.host = info["endpoint"]
    cfg.ssl_ca_cert = ca.name
    cfg.api_key = {"authorization": "Bearer " + eks_token(cluster_name, region)}
    return client.ApiClient(cfg)


def mgmt_api_client() -> client.ApiClient:
    """In an Argo Workflows pod: in-cluster config. On the mgmt host: kubeconfig."""
    if os.getenv("KUBERNETES_SERVICE_HOST") and not os.getenv("CM_MGMT_CONTEXT"):
        kconfig.load_incluster_config()
        return client.ApiClient()
    return kconfig.new_client_from_config(context=os.getenv("CM_MGMT_CONTEXT", "cm-mgmt"))


class KubeClients:
    """Per-colour API clients with token refresh."""

    TTL_SECONDS = 600

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._cache: dict[str, tuple[float, client.ApiClient]] = {}

    def api(self, color: str) -> client.ApiClient:
        cached = self._cache.get(color)
        if cached and time.time() - cached[0] < self.TTL_SECONDS:
            return cached[1]
        api = mgmt_api_client() if color == "mgmt" else eks_api_client(
            self.cfg["clusters"][color], self.cfg["region"])
        self._cache[color] = (time.time(), api)
        return api

    def core(self, color: str) -> client.CoreV1Api:
        return client.CoreV1Api(self.api(color))

    def apps(self, color: str) -> client.AppsV1Api:
        return client.AppsV1Api(self.api(color))

    def batch(self, color: str) -> client.BatchV1Api:
        return client.BatchV1Api(self.api(color))

    def custom(self, color: str) -> client.CustomObjectsApi:
        return client.CustomObjectsApi(self.api(color))

    def version(self, color: str) -> str:
        return client.VersionApi(self.api(color)).get_code().git_version


def wait_for(predicate, timeout: float, interval: float = 5.0, desc: str = "condition"):
    """Poll predicate() until it returns a truthy value; return that value."""
    deadline = time.time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.time() > deadline:
            raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {desc}")
        time.sleep(interval)
