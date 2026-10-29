"""Turn Prometheus query results into hosts with checks and metrics.

Pure Python without Home Assistant imports, so it can be tested on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re

from .const import (
    DEFAULT_REASON,
    EXPORTER_JOBS,
    NODE_EXPORTER_JOB,
    NODE_NAME_STRIP_PREFIX,
    REASONS,
    STATUS_DOWN,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNKNOWN,
)

_IPV4 = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


@dataclass
class Check:
    """One check of a host (blackbox probe, exporter or database state)."""

    key: str
    service: str
    module: str
    target: str
    ok: bool
    since: datetime | None = None

    @property
    def reason(self) -> str | None:
        return None if self.ok else REASONS.get(self.module, DEFAULT_REASON)

    @property
    def short_target(self) -> str:
        """Target without scheme and path, e.g. 10.0.0.5:443."""
        t = re.sub(r"^[a-z]+://", "", self.target)
        return t.split("/")[0]


@dataclass
class Host:
    """A monitored host with everything Prometheus knows about it."""

    key: str
    name: str
    site: str = ""
    role: str = ""
    tenant: str = ""
    hyperv: str = ""
    ip: str = ""
    checks: dict[str, Check] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        """Device kind derived from the role label and the services."""
        services = {c.service for c in self.checks.values()}
        role = self.role.lower()
        if "plattform:proxmox-ve" in services:
            return "Proxmox"
        if role.startswith("usv") or "rolle:usv" in services:
            return "USV"
        if role == "ap" or "rolle:ap" in services:
            return "Access Point"
        if role.startswith("switch") or "switch" in services:
            return "Switch"
        if role == "cloudkey":
            return "Cloudkey"
        if role == "virtuelle-maschine":
            return "VM"
        if role == "bare-metal":
            return "Hyper-V" if "HV" in self.key else "Hardware"
        if role == "server":
            return "Server"
        return "Sonstige"

    @property
    def checks_ok(self) -> int:
        return sum(1 for c in self.checks.values() if c.ok)

    @property
    def status(self) -> str:
        if not self.checks:
            return STATUS_UNKNOWN
        ok = self.checks_ok
        if ok == len(self.checks):
            return STATUS_OK
        return STATUS_DOWN if ok == 0 else STATUS_PARTIAL


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _value(row: dict) -> float | None:
    try:
        return float(row["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


class _Builder:
    def __init__(self) -> None:
        self.hosts: dict[str, Host] = {}

    def host(self, instance: str, labels: dict | None = None, create: bool = True) -> Host | None:
        key = instance.upper()
        h = self.hosts.get(key)
        if h is None:
            if not create:
                return None
            h = self.hosts[key] = Host(key=key, name=instance)
        if labels:
            h.site = h.site or (labels.get("site") or "").upper()
            h.role = h.role or labels.get("rolle", "")
            h.tenant = h.tenant or labels.get("tenant", "")
            h.hyperv = h.hyperv or (labels.get("cluster") or "").replace("Hyper-V ", "").upper()
        return h

    def add_check(self, h: Host, service: str, module: str, target: str, ok: bool) -> None:
        key = _slug(f"{service}_{module}_{target}") or "check"
        old = h.checks.get(key)
        if old:
            old.ok = old.ok and ok
        else:
            h.checks[key] = Check(key=key, service=service, module=module, target=target, ok=ok)


def build_hosts(results: dict[str, list[dict]], alerts: list[dict] | None = None) -> dict[str, Host]:
    """Build hosts from query results keyed like const.QUERIES and the active alerts."""
    b = _Builder()

    for row in results.get("checks", []):
        m = row.get("metric", {})
        if not m.get("instance"):
            continue
        h = b.host(m["instance"], m)
        target = m.get("target", "")
        host_part = target.split(":")[0]
        if not h.ip and _IPV4.match(host_part):
            h.ip = host_part
        b.add_check(h, m.get("dienst", "?"), m.get("modul", ""), target, _value(row) == 1)

    # Cluster nodes: map ip:port instances to their node name.
    node_names: dict[str, str] = {}
    for row in results.get("uname", []):
        m = row.get("metric", {})
        if m.get("instance") and m.get("nodename"):
            name = m["nodename"]
            if name.startswith(NODE_NAME_STRIP_PREFIX):
                name = name[len(NODE_NAME_STRIP_PREFIX):]
            node_names[m["instance"]] = name

    def resolve(instance: str) -> str:
        return node_names.get(instance, instance)

    for row in results.get("up", []):
        m = row.get("metric", {})
        job, inst = m.get("job", ""), m.get("instance", "")
        if not inst:
            continue
        if job in EXPORTER_JOBS:
            h = b.host(inst, m)
            b.add_check(h, EXPORTER_JOBS[job], "exporter", "", _value(row) == 1)
        elif job == NODE_EXPORTER_JOB and inst in node_names:
            h = b.host(node_names[inst], m)
            if not h.ip:
                h.ip = inst.split(":")[0]
            b.add_check(h, "Node-Exporter", "exporter", "", _value(row) == 1)

    for row in results.get("db", []):
        m = row.get("metric", {})
        h = b.host(m.get("instance", ""), create=False) if m.get("instance") else None
        v = _value(row)
        if h is None or v is None:
            continue
        name = m.get("__name__", "")
        if name == "mysql_up":
            b.add_check(h, "MariaDB", "db", "", v == 1)
        elif name == "mysql_global_status_wsrep_ready":
            b.add_check(h, "Galera-Cluster", "galera", "", v == 1)
        elif name == "mysql_global_status_wsrep_cluster_size":
            h.metrics["galera_cluster_size"] = v
        elif name == "proxysql_mysql_listener_paused":
            b.add_check(h, "ProxySQL", "proxy", "", v != 1)
        elif name == "proxysql_connpool_conns_status":
            b.add_check(h, "ProxySQL-Backend", "backend", m.get("endpoint", "?"), v == 1)

    metric_queries = {
        "response_time": ["duration"],
        "cpu": ["cpu", "win_cpu"],
        "ram": ["ram", "win_ram"],
        "disk": ["disk", "win_disk"],
    }
    for metric, queries in metric_queries.items():
        for q in queries:
            for row in results.get(q, []):
                inst = row.get("metric", {}).get("instance")
                v = _value(row)
                if not inst or v is None:
                    continue
                h = b.host(resolve(inst), create=False)
                if h is not None:
                    h.metrics[metric] = round(v, 1)

    for a in alerts or []:
        labels = a.get("labels", {})
        if a.get("state") != "firing" or not labels.get("instance"):
            continue
        h = b.host(resolve(labels["instance"]), create=False)
        if h is None:
            continue
        try:
            since = datetime.fromisoformat(a["activeAt"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        for c in h.checks.values():
            if c.ok:
                continue
            if labels.get("target") and labels["target"] != c.target:
                continue
            if c.since is None or since < c.since:
                c.since = since

    return b.hosts
