"""Tests for parse.build_hosts with invented values (no real host names or addresses).

Each scenario is run twice with changed values to make sure state changes,
new hosts and vanished checks are picked up from one poll to the next.
"""

from pathlib import Path
import sys
import types

# Load const/parse without executing the package __init__ (which needs Home Assistant).
_PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "prometheus_hosts"
_pkg = types.ModuleType("ph")
_pkg.__path__ = [str(_PKG_DIR)]
sys.modules["ph"] = _pkg

from ph.parse import build_hosts, build_overview  # noqa: E402


def probe(instance, dienst, modul, target, value, **labels):
    metric = {"__name__": "probe_success", "job": "blackbox-sd", "instance": instance,
              "dienst": dienst, "modul": modul, "target": target, **labels}
    return {"metric": metric, "value": [0, str(value)]}


def row(value, **metric):
    return {"metric": metric, "value": [0, str(value)]}


VM_LABELS = {"site": "lab", "rolle": "virtuelle-maschine", "cluster": "Hyper-V LABHV01", "tenant": "t1"}


def base_results(web_ok=1, dns_ok=1, hv_ok=1, mysql_up=1, paused=0, win_up=1):
    return {
        "checks": [
            probe("VM-WEB01", "web", "http_2xx", "https://192.0.2.10/health", web_ok, **VM_LABELS),
            probe("VM-WEB01", "ping", "icmp", "192.0.2.10", 1, **VM_LABELS),
            # Same check reported by a second job: must merge into one check.
            {**probe("VM-WEB01", "ping", "icmp", "192.0.2.10", 1, **VM_LABELS)},
            probe("VM-DNS01", "dns", "dns_soa", "192.0.2.20:53", dns_ok, site="lab",
                  rolle="virtuelle-maschine", cluster="LABHV01"),
            probe("LABHV01", "ping", "icmp", "192.0.2.1", hv_ok, site="lab", rolle="bare-metal"),
            probe("DB-NODE1", "ping", "icmp", "192.0.2.30", 1, site="lab", rolle="server"),
        ],
        "up": [
            row(win_up, job="windows-exporter", instance="labhv01"),
            row(1, job="mysqld-exporter", instance="DB-NODE1"),
            row(1, job="proxysql", instance="DB-NODE1"),
            row(1, job="node-exporter", instance="198.51.100.5:9100"),
            row(1, job="kubelet", instance="198.51.100.5:10250"),
        ],
        "uname": [row(1, instance="198.51.100.5:9100", nodename="rke2-node-a")],
        "duration": [row(12.34, instance="VM-WEB01"), row(4000, instance="VM-DNS01")],
        "cpu": [row(37.26, instance="198.51.100.5:9100")],
        "ram": [row(55.0, instance="198.51.100.5:9100")],
        "win_disk": [row(71.55, instance="LABHV01")],
        "targets_down": [row(0 if win_up else 1)],
        "nodes_not_ready": [row(0)],
        "pods_failed": [row(2)],
        "probes_failed": [row(3 - web_ok - dns_ok - hv_ok)],
        "cert_expiry": [
            row(86400 * 90, instance="VM-WEB01", dienst="web", target="web.example.test:443"),
            row(86400 * 12.4, instance="VM-DNS01", dienst="dns", target="dns.example.test:853"),
        ],
        "db": [
            row(mysql_up, __name__="mysql_up", instance="DB-NODE1"),
            row(3, __name__="mysql_global_status_wsrep_cluster_size", instance="DB-NODE1"),
            row(paused, __name__="proxysql_mysql_listener_paused", instance="DB-NODE1"),
            row(1, __name__="proxysql_connpool_conns_status", instance="DB-NODE1", endpoint="192.0.2.31:3306"),
            row(1, __name__="mysql_up", instance="UNKNOWN-HOST"),
        ],
    }


def test_hosts_and_metadata():
    hosts = build_hosts(base_results())
    assert set(hosts) == {"VM-WEB01", "VM-DNS01", "LABHV01", "DB-NODE1", "NODE-A"}
    web = hosts["VM-WEB01"]
    assert (web.site, web.kind, web.hyperv, web.ip, web.tenant) == ("LAB", "VM", "LABHV01", "192.0.2.10", "t1")
    assert len(web.checks) == 2
    assert web.metrics["response_time"] == 12.3
    assert hosts["VM-DNS01"].hyperv == "LABHV01"
    assert hosts["LABHV01"].kind == "Hyper-V"
    assert hosts["LABHV01"].metrics["disk"] == 71.5 or hosts["LABHV01"].metrics["disk"] == 71.6
    node = hosts["NODE-A"]
    assert node.ip == "198.51.100.5" and node.metrics == {"cpu": 37.3, "ram": 55.0}
    assert "UNKNOWN-HOST" not in hosts


def test_exporter_merges_case_insensitive():
    hv = build_hosts(base_results())["LABHV01"]
    services = {c.service for c in hv.checks.values()}
    assert services == {"ping", "Windows-Exporter"}
    assert hv.name == "LABHV01"


def test_database_checks():
    db = build_hosts(base_results())["DB-NODE1"]
    services = sorted(c.service for c in db.checks.values())
    assert services == ["MariaDB", "MariaDB-Exporter", "ProxySQL", "ProxySQL-Backend",
                        "ProxySQL-Exporter", "ping"]
    assert db.metrics["galera_cluster_size"] == 3
    assert db.status == "ok"


def test_changed_values_between_polls():
    first = build_hosts(base_results())
    assert first["VM-WEB01"].status == "ok"
    assert first["LABHV01"].status == "ok"

    second = build_hosts(base_results(web_ok=0, hv_ok=0, win_up=0, mysql_up=0, paused=1))
    web = second["VM-WEB01"]
    assert web.status == "teilweise" and web.checks_ok == 1
    failing = [c for c in web.checks.values() if not c.ok][0]
    assert failing.reason == "HTTP-Fehler" and failing.short_target == "192.0.2.10"
    assert second["LABHV01"].status == "ausgefallen"
    db = second["DB-NODE1"]
    assert {c.service for c in db.checks.values() if not c.ok} == {"MariaDB", "ProxySQL"}
    assert db.status == "teilweise"

    third = build_hosts(base_results())
    assert all(h.status == "ok" for h in third.values())


def test_duplicate_series_one_failing_marks_check_failed():
    res = base_results()
    res["checks"][2] = probe("VM-WEB01", "ping", "icmp", "192.0.2.10", 0, **VM_LABELS)
    web = build_hosts(res)["VM-WEB01"]
    assert web.checks_ok == 1 and len(web.checks) == 2


def test_new_and_vanished_hosts():
    res = base_results()
    res["checks"].append(probe("NEW-SW01", "switch", "icmp", "192.0.2.40", 1, site="lab2"))
    hosts = build_hosts(res)
    assert hosts["NEW-SW01"].kind == "Switch" and hosts["NEW-SW01"].site == "LAB2"

    res = base_results()
    res["checks"] = [c for c in res["checks"] if c["metric"]["instance"] != "VM-DNS01"]
    res["duration"] = res["duration"][:1]
    assert "VM-DNS01" not in build_hosts(res)


def test_alert_sets_since_only_on_matching_failing_check():
    alerts = [
        {"state": "firing", "activeAt": "2026-01-02T03:04:05Z",
         "labels": {"instance": "VM-WEB01", "target": "https://192.0.2.10/health"}},
        {"state": "pending", "activeAt": "2026-01-01T00:00:00Z",
         "labels": {"instance": "VM-WEB01"}},
    ]
    web = build_hosts(base_results(web_ok=0), alerts)["VM-WEB01"]
    failing = [c for c in web.checks.values() if not c.ok][0]
    ok = [c for c in web.checks.values() if c.ok][0]
    assert failing.since.isoformat() == "2026-01-02T03:04:05+00:00"
    assert ok.since is None


def test_empty_results():
    assert build_hosts({}) == {}
    h = build_hosts({"checks": [probe("X1", "ping", "icmp", "bad", "NaN")]})["X1"]
    assert h.status == "ausgefallen"


AM_ALERTS = [
    {"labels": {"alertname": "Watchdog", "severity": "none"}, "status": {"state": "active"}},
    {"labels": {"alertname": "HostDown", "severity": "critical"}, "status": {"state": "active"}},
    {"labels": {"alertname": "DiskFull", "severity": "warning"}, "status": {"state": "active"}},
    {"labels": {"alertname": "Silenced", "severity": "critical"}, "status": {"state": "suppressed"}},
]


def test_overview_numbers_and_certs():
    o = build_overview(base_results(), AM_ALERTS)
    assert (o.targets_down, o.nodes_not_ready, o.pods_failed, o.probes_failed) == (0, 0, 2, 0)
    assert o.certs_checked == 2 and o.cert_min_days == 12.4
    assert o.certs_expiring == ["dns VM-DNS01 12 d"]
    assert o.certs_next == [
        {"dienst": "dns", "instance": "VM-DNS01", "target": "dns.example.test:853", "tage": 12},
        {"dienst": "web", "instance": "VM-WEB01", "target": "web.example.test:443", "tage": 90},
    ]
    assert (o.alerts_open, o.alerts_critical, o.alerts_warning) == (2, 1, 1)
    assert o.alert_names == ["DiskFull", "HostDown"]
    assert o.watchdog_missing is False


def test_overview_changed_values():
    o = build_overview(base_results(web_ok=0, dns_ok=0, win_up=0), AM_ALERTS[1:2])
    assert o.targets_down == 1 and o.probes_failed == 2
    assert o.watchdog_missing is True
    assert o.alerts_open == 1 and o.alert_names == ["HostDown"]
    o = build_overview({}, [])
    assert o.certs_checked == 0 and o.cert_min_days is None and o.alerts_open == 0


def test_overview_from_prometheus_alert_format():
    alerts = [{"labels": {"alertname": "Watchdog"}, "state": "firing"},
              {"labels": {"alertname": "X", "severity": "warning"}, "state": "pending"}]
    o = build_overview({}, alerts)
    assert o.watchdog_missing is False and o.alerts_open == 0


def test_check_names_unique_per_host():
    res = {"checks": [
        probe("MAIL01", "mail", "icmp", "192.0.2.48", 1),
        probe("MAIL01", "mail", "tcp_connect", "192.0.2.48:25", 1),
        probe("MAIL01", "mail", "http_2xx", "https://192.0.2.48/owa/health", 1),
        probe("MAIL01", "mail", "http_2xx", "https://192.0.2.48/ecp/health", 1),
        probe("MAIL01", "mail", "tls_connect", "192.0.2.48", 1),
        probe("MAIL01", "web", "http_2xx", "https://www.example.test/", 1),
    ]}
    names = sorted(c.name for c in build_hosts(res)["MAIL01"].checks.values())
    assert names == [
        "mail · 192.0.2.48 (HTTP) /ecp/health",
        "mail · 192.0.2.48 (HTTP) /owa/health",
        "mail · 192.0.2.48 (Ping)",
        "mail · 192.0.2.48 (TLS)",
        "mail · 192.0.2.48:25",
        "web · www.example.test",
    ]
    db_names = {c.name for c in build_hosts(base_results())["DB-NODE1"].checks.values()}
    assert "ProxySQL-Backend · 192.0.2.31:3306" in db_names and "MariaDB" in db_names


def test_overview_host_counts_follow_changes():
    hosts = build_hosts(base_results())
    o = build_overview(base_results(), [], hosts)
    assert (o.hosts_total, o.hosts_down, o.hosts_partial) == (5, 0, 0)
    assert o.checks_total == sum(len(h.checks) for h in hosts.values())

    res = base_results(web_ok=0, hv_ok=0, win_up=0)
    o = build_overview(res, [], build_hosts(res))
    assert o.hosts_down_names == ["LABHV01"]
    assert o.hosts_partial_names == ["VM-WEB01"]
    assert (o.hosts_down, o.hosts_partial) == (1, 1)


def test_certs_next_is_capped_and_follows_changing_values():
    rows = [row(86400 * (100 - n), instance=f"VM-{n:02d}", dienst="web") for n in range(40)]
    o = build_overview({"cert_expiry": rows}, [])
    assert len(o.certs_next) == 25
    assert o.certs_next[0] == {"dienst": "web", "instance": "VM-39", "target": "?", "tage": 61}
    rows[0] = row(86400 * 2, instance="VM-00", dienst="web")
    o = build_overview({"cert_expiry": rows}, [])
    assert o.certs_next[0]["instance"] == "VM-00" and o.certs_next[0]["tage"] == 2
    assert o.certs_expiring == ["web VM-00 2 d"]
