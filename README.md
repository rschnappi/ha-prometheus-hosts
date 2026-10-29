# Prometheus Hosts

Home Assistant integration that turns every host known to Prometheus into a **device**, with all of its checks and
metrics as entities underneath it. New hosts appear automatically, vanished hosts become unavailable and are removed
after a grace period.

## What you get per host

| Entity | Type | Content |
|---|---|---|
| Status | sensor (enum) | `ok`, `teilweise` (some checks fail), `ausgefallen` (all fail); site, kind, IP, hypervisor as attributes |
| Pruefungen ok | sensor | number of passing checks; total and failing services as attributes |
| `<service> · <target>` | binary_sensor (connectivity) | one per check, `on` = ok; reason and `seit` (alert start) when failing; on a name clash the check type (Ping, Port, TLS, HTTP ...) and then the URL path are added |
| Antwortzeit | sensor (ms) | mean blackbox probe duration |
| CPU / RAM / Disk | sensor (%) | from node_exporter or windows_exporter, only when available |
| Galera-Knoten | sensor | Galera cluster size (MariaDB hosts) |

**Linked devices:** since Home Assistant 2026.8 devices of different integrations are no longer merged, so the host
device shows the values of matching devices itself, updated immediately when the source changes, and links to the
source device page.

- **APC UPS** (`apc_modbus`): matched by entry title, else by the host IP in the entry. Shows USV Ladung,
  Restlaufzeit, Last, Batterietemperatur, Eingangsspannung, Letzter Selbsttest, Online, Auf Batterie, Batterie tauschen.
- **UniFi:** if the UniFi Controller integration (`unifi_controller`) is installed, each host is matched to its UniFi
device by name (a name prefix is ignored), otherwise by IP. The host device then also shows UniFi Status, Firmware,
Gestartet, Clients, CPU, Speicher and Temperatur (whatever the UniFi device has), updated immediately when the UniFi
entity changes, and links to the UniFi device page. Since Home Assistant 2026.8 devices of different integrations are
no longer merged, so this is how both sources end up on one device page.

An extra device **Monitoring** holds the overall numbers: hosts total / down / partially down (with names), number of checks, open/critical/warning alerts (with names), `Watchdog fehlt`
(alert pipeline broken), targets down, Kubernetes nodes not ready, failed pods, failed probes, next certificate
expiry in days (certificates under 30 days as attribute) and number of checked certificates. With an Alertmanager URL
configured, alerts come from Alertmanager (silenced and inhibited ones excluded), otherwise from Prometheus.

Devices carry the kind as model (VM, Hyper-V, Proxmox, Switch, USV, Access Point, Server, ...), the `site` label as
suggested area, and VMs are linked to their Hyper-V host (`via_device_id`) - the host's device page lists its VMs.

## Data sources (read-only)

- `probe_success{job=~"blackbox.*"}` with labels `instance`, `dienst`, `modul`, `target`, `site`, `rolle`, `cluster`, `tenant`
- `up` of the jobs `windows-exporter`, `node-exporter-linux`, `mysqld-exporter`, `proxysql`; `node-exporter` via `node_uname_info`
- node_exporter / windows_exporter CPU, memory and filesystem metrics
- `mysql_up`, `mysql_global_status_wsrep_*`, `proxysql_*`
- `/api/v1/alerts` for the start time of failing checks

The queries live in `custom_components/prometheus_hosts/const.py`.

## Installation

HACS -> Custom repositories -> this repository (category Integration), install, restart Home Assistant, then
Settings -> Devices & services -> Add integration -> Prometheus Hosts. Enter the Prometheus URL and, if needed,
basic-auth user and password (stored in the config entry, not in YAML), optionally the Alertmanager URL (same
credentials). The icon ships in `brand/` (Home Assistant 2026.3+).

Settings -> Devices & services -> Prometheus Hosts:

- **Configure** (options, applied at once): polling interval (default 60 s), hours after which vanished hosts are
  removed (default 24), show UniFi values and show APC UPS values on the host device (both on by default).
- **Reconfigure**: Prometheus URL, user, password (empty keeps the current one), Alertmanager URL, certificate check.

## Releases

Every push runs `.github/workflows/ci.yml` (lint, pytest, hassfest, HACS). On success a version tag is created from
the commit messages (`feat` -> minor, `BREAKING` -> major, otherwise patch; `[skip release]` or docs/workflow-only
changes -> no release) and a GitHub release with `prometheus_hosts.zip`; HACS installs these releases.

## Tests

`tests/test_parse.py` runs without Home Assistant (`pytest tests/test_parse.py`); `tests/test_integration.py` needs
`pytest-homeassistant-custom-component` (Linux, see the GitHub workflow). All test data is invented.
