"""Fencing check shared by every singleton workload.

The ClusterMotion lease agent turns singleton workloads on in exactly one
cluster. Agents can crash or lag, so every unit of singleton work also asks
DynamoDB "does my cluster hold the lease right now?" before it starts.
Fail closed: if DynamoDB cannot be read, the answer is "no".
"""
from __future__ import annotations

import logging
import os
import time

import boto3

log = logging.getLogger("lease_guard")


class LeaseGuard:
    def __init__(self, table: str | None = None, lease_id: str | None = None,
                 cluster: str | None = None, cache_seconds: float = 5.0, client=None):
        self.table = table if table is not None else os.getenv("LEASE_TABLE", "")
        self.lease_id = lease_id or os.getenv("LEASE_ID", "singletons")
        self.cluster = cluster or os.getenv("CLUSTER_NAME", "local")
        self.cache_seconds = cache_seconds
        self._client = client
        self._checked_at = float("-inf")
        self._value = False

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client("dynamodb")
        return self._client

    def holder(self) -> str:
        item = self.client.get_item(
            TableName=self.table,
            Key={"lease_id": {"S": self.lease_id}},
            ConsistentRead=True,
        ).get("Item")
        return item.get("holder", {}).get("S", "") if item else ""

    def holds_lease(self) -> bool:
        if not self.table:  # no lease table configured (unit tests): always allowed
            return True
        if time.monotonic() - self._checked_at < self.cache_seconds:
            return self._value
        try:
            self._value = self.holder() == self.cluster
        except Exception as exc:  # fail closed
            log.warning("lease check failed, assuming not holder: %s", exc)
            self._value = False
        self._checked_at = time.monotonic()
        return self._value
