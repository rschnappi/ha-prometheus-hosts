"""Turn Prometheus query results into hosts with checks and metrics.

Pure Python without Home Assistant imports, so it can be tested on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re

from .const import (
    CERT_NEXT_MAX,
    CERT_WARN_DAYS,
    DEFAULT_REASON,
    EXPORTER_JOBS,
    MODULE_LABELS,
    NODE_EXPORTER_JOB,
    NODE_NAME_STRIP_PREFIX,
    REASONS,
    STATUS_DOWN,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNKNOWN,
    WATCHDOG_ALERT,
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
    name: str = ""

    @property
    def reason(self) -> str | None:
        return None if self.ok else REASONS.get(self.module, DEFAULT_REASON)

    @property
    def short_target(self) -> str:
        """Target without scheme and path, e.g. 10.0.0.5:443."""
        t = re.sub(r"^[a-z]+://", "", self.target)
        return t.split("/")[0]

    @property
    def path(self) -> str:
        """Path part of an URL target, e.g. /owa/healthcheck.htm."""
        t = re.sub(r"^[a-z]+://", "", self.target)
        return "/" + t.split("/", 1)[1] if "/" in t else ""


def _assign_names(checks: dict[str, Check]) -> None:
    """Unique display names per host: service and target; on a clash add the check type, then the path."""
    for c in checks.values():
        c.name = f"{c.service} · {c.short_target}" if c.short_target else c.service

    def clashes() -> list[Check]:
        count: dict[str, int] = {}
        for c in checks.values():
            count[c.name] = count.get(c.name, 0) + 1
        return [c for c in checks.values() if count[c.name] > 1]

    for c in clashes():
        if c.module:
            c.name = f"{c.name} ({MODULE_LABELS.get(c.module, c.module)})"
    for c in clashes():
        if c.path:
            c.name = f"{c.name} {c.path}"


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

    for h in b.hosts.values():
        _assign_names(h.checks)
    return b.hosts


@dataclass
class Overview:
    """Cluster-wide numbers for the Monitoring device."""

    targets_down: int = 0
    nodes_not_ready: int = 0
    pods_failed: int = 0
    probes_failed: int = 0
    certs_checked: int = 0
    cert_min_days: float | None = None
    certs_expiring: list[str] = field(default_factory=list)
    certs_next: list[dict] = field(default_factory=list)
    alerts_open: int = 0
    alerts_critical: int = 0
    alerts_warning: int = 0
    alert_names: list[str] = field(default_factory=list)
    watchdog_missing: bool = True
    hosts_total: int = 0
    hosts_down: int = 0
    hosts_partial: int = 0
    checks_total: int = 0
    hosts_down_names: list[str] = field(default_factory=list)
    hosts_partial_names: list[str] = field(default_factory=list)


def firing_alert_labels(alerts: list[dict]) -> list[dict]:
    """Labels of active alerts from Alertmanager (v2) or Prometheus (/api/v1/alerts)."""
    out = []
    for a in alerts or []:
        status = a.get("status")
        if isinstance(status, dict):
            if status.get("state") != "active":
                continue
        elif a.get("state") != "firing":
            continue
        out.append(a.get("labels", {}))
    return out


def build_overview(
    results: dict[str, list[dict]], alerts: list[dict] | None, hosts: dict[str, Host] | None = None
) -> Overview:
    o = Overview()

    for h in sorted((hosts or {}).values(), key=lambda h: h.name.upper()):
        o.hosts_total += 1
        o.checks_total += len(h.checks)
        if h.status == STATUS_DOWN:
            o.hosts_down_names.append(h.name)
        elif h.status == STATUS_PARTIAL:
            o.hosts_partial_names.append(h.name)
    o.hosts_down = len(o.hosts_down_names)
    o.hosts_partial = len(o.hosts_partial_names)

    def scalar(name: str) -> int:
        rows = results.get(name) or []
        v = _value(rows[0]) if rows else None
        return int(v) if v is not None and v == v else 0

    o.targets_down = scalar("targets_down")
    o.nodes_not_ready = scalar("nodes_not_ready")
    o.pods_failed = scalar("pods_failed")
    o.probes_failed = scalar("probes_failed")

    certs = []
    for row in results.get("cert_expiry", []):
        v = _value(row)
        if v is None:
            continue
        m = row.get("metric", {})
        certs.append(
            (v / 86400, m.get("dienst") or m.get("instance") or "?", m.get("instance", "?"), m.get("target", "?"))
        )
    certs.sort()
    o.certs_checked = len(certs)
    o.cert_min_days = round(certs[0][0], 1) if certs else None
    o.certs_expiring = [f"{d} {i} {round(days)} d" for days, d, i, _ in certs if days < CERT_WARN_DAYS]
    o.certs_next = [
        {"dienst": d, "instance": i, "target": t, "tage": round(days)} for days, d, i, t in certs[:CERT_NEXT_MAX]
    ]

    labels = firing_alert_labels(alerts or [])
    o.watchdog_missing = not any(l.get("alertname") == WATCHDOG_ALERT for l in labels)
    real = [l for l in labels if l.get("alertname") != WATCHDOG_ALERT]
    o.alerts_open = len(real)
    o.alerts_critical = sum(1 for l in real if l.get("severity") == "critical")
    o.alerts_warning = sum(1 for l in real if l.get("severity") == "warning")
    o.alert_names = sorted({l.get("alertname", "?") for l in real})
    return o
