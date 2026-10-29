"""Constants for Prometheus Hosts."""

DOMAIN = "prometheus_hosts"

CONF_VERIFY_SSL = "verify_ssl"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_STALE_HOURS = "stale_hours"

DEFAULT_SCAN_INTERVAL = 60
DEFAULT_STALE_HOURS = 24

# PromQL queries; every result is matched to a host via its "instance" label.
QUERIES = {
    "checks": 'probe_success{job=~"blackbox.*"}',
    "duration": 'avg by (instance) (probe_duration_seconds{job=~"blackbox.*"}) * 1000',
    "up": "up",
    "uname": "node_uname_info",
    "cpu": '100 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100',
    "ram": "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100",
    "disk": (
        'max by (instance) ((1 - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs|nsfs|ramfs"}'
        ' / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs|nsfs|ramfs"}) * 100)'
    ),
    "win_cpu": '100 - avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100',
    "win_ram": (
        "(1 - windows_memory_physical_free_bytes / windows_memory_physical_total_bytes) * 100"
        " or (1 - windows_os_physical_memory_free_bytes / windows_cs_physical_memory_bytes) * 100"
    ),
    "win_disk": (
        "max by (instance) ((1 - windows_logical_disk_free_bytes / windows_logical_disk_size_bytes) * 100)"
    ),
    "db": (
        '{__name__=~"mysql_up|mysql_global_status_wsrep_ready|mysql_global_status_wsrep_cluster_size'
        '|proxysql_mysql_listener_paused|proxysql_connpool_conns_status"}'
    ),
}

# Exporter jobs that become a check on the host named by their "instance" label.
EXPORTER_JOBS = {
    "windows-exporter": "Windows-Exporter",
    "node-exporter-linux": "Linux-Exporter",
    "mysqld-exporter": "MariaDB-Exporter",
    "proxysql": "ProxySQL-Exporter",
}
# Cluster node exporter: instance is ip:port, the host name comes from node_uname_info.
NODE_EXPORTER_JOB = "node-exporter"
NODE_NAME_STRIP_PREFIX = "rke2-"

# Reason shown for a failing check, by blackbox module or internal check type.
REASONS = {
    "icmp": "antwortet nicht auf Ping",
    "tcp_connect": "Port antwortet nicht",
    "tls_connect": "TLS-Verbindung fehlgeschlagen",
    "dns_soa": "DNS-Abfrage fehlgeschlagen",
    "ldap_tcp": "LDAP-Verbindung fehlgeschlagen",
    "http_2xx": "HTTP-Fehler",
    "exporter": "Exporter antwortet nicht",
    "db": "Datenbank nicht erreichbar",
    "galera": "nicht im Cluster bereit",
    "proxy": "Listener pausiert",
    "backend": "Backend nicht ONLINE",
}
DEFAULT_REASON = "Pruefung fehlgeschlagen"

STATUS_OK = "ok"
STATUS_PARTIAL = "teilweise"
STATUS_DOWN = "ausgefallen"
STATUS_UNKNOWN = "unbekannt"
STATUS_OPTIONS = [STATUS_OK, STATUS_PARTIAL, STATUS_DOWN, STATUS_UNKNOWN]
