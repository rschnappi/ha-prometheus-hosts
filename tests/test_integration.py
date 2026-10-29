"""Home Assistant tests with a mocked Prometheus and invented, changing values."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.prometheus_hosts.api import PrometheusAuthError
from custom_components.prometheus_hosts.const import (
    CONF_ALERTMANAGER_URL,
    CONF_STALE_HOURS,
    DOMAIN,
    OVERVIEW_KEY,
)

from .test_parse import AM_ALERTS, base_results, probe

CLIENT = "custom_components.prometheus_hosts.api.PrometheusClient"
URL = "https://prometheus.example.test"


class FakePrometheus:
    """Holds the next poll result; tests change it between refreshes."""

    def __init__(self) -> None:
        self.results = base_results()
        self.alerts = []
        self.am_alerts = []

    async def query_all(self, queries):
        return self.results

    async def get_alerts(self):
        return self.alerts

    async def get_am_alerts(self, url):
        return self.am_alerts


@pytest.fixture
def prom():
    fake = FakePrometheus()
    with (
        patch(f"{CLIENT}.query_all", side_effect=fake.query_all),
        patch(f"{CLIENT}.alerts", side_effect=fake.get_alerts),
        patch(f"{CLIENT}.alertmanager_alerts", side_effect=fake.get_am_alerts),
        patch(f"{CLIENT}.query", AsyncMock(return_value=[])),
    ):
        yield fake


async def setup_entry(hass: HomeAssistant, options=None, data=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=URL,
        data=data or {CONF_URL: URL, CONF_USERNAME: "user", CONF_PASSWORD: "pw"},
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def entity_id(hass, entry, platform, host, suffix):
    return er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}_{host}_{suffix}"
    )


def get_device(hass, key):
    for dev in dr.async_get(hass).devices:
        if (DOMAIN, key) in dev.identifiers:
            return dev
    return None


async def refresh(hass, entry):
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_config_flow(hass: HomeAssistant, prom) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    with patch(f"{CLIENT}.query", AsyncMock(side_effect=PrometheusAuthError("401"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: URL + "/", CONF_USERNAME: "u", CONF_PASSWORD: "bad"}
        )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL + "/", CONF_USERNAME: "u", CONF_PASSWORD: "ok"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_URL] == URL


async def test_devices_and_entities(hass: HomeAssistant, prom) -> None:
    entry = await setup_entry(hass)
    devreg = dr.async_get(hass)

    devices = {
        next(k for d, k in dev.identifiers if d == DOMAIN): dev
        for dev in dr.async_entries_for_config_entry(devreg, entry.entry_id)
        if (DOMAIN, OVERVIEW_KEY) not in dev.identifiers
    }
    assert set(devices) == {"VM-WEB01", "VM-DNS01", "LABHV01", "DB-NODE1", "NODE-A"}
    assert devices["VM-WEB01"].via_device_id == devices["LABHV01"].id
    assert devices["VM-DNS01"].via_device_id == devices["LABHV01"].id
    assert devices["VM-WEB01"].model == "VM"
    assert devices["LABHV01"].model == "Hyper-V"

    status = hass.states.get(entity_id(hass, entry, "sensor", "VM-WEB01", "status"))
    assert status.state == "ok"
    assert status.attributes["hyperv_host"] == "LABHV01"
    rt = hass.states.get(entity_id(hass, entry, "sensor", "VM-WEB01", "response_time"))
    assert float(rt.state) == 12.3
    galera = hass.states.get(entity_id(hass, entry, "sensor", "DB-NODE1", "galera_cluster_size"))
    assert float(galera.state) == 3
    # No CPU sensor for hosts without that metric.
    assert entity_id(hass, entry, "sensor", "VM-WEB01", "cpu") is None

    checks = [
        e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.domain == "binary_sensor" and f"_VM-WEB01_" in e.unique_id
    ]
    assert len(checks) == 2
    assert all(hass.states.get(e.entity_id).state == "on" for e in checks)


async def test_changed_values(hass: HomeAssistant, prom) -> None:
    entry = await setup_entry(hass)
    status_id = entity_id(hass, entry, "sensor", "VM-WEB01", "status")
    web_check = entity_id(hass, entry, "binary_sensor", "VM-WEB01", "check_web_http_2xx_https_192_0_2_10_health")
    hv_status = entity_id(hass, entry, "sensor", "LABHV01", "status")
    ok_id = entity_id(hass, entry, "sensor", "DB-NODE1", "checks_ok")

    prom.results = base_results(web_ok=0, hv_ok=0, win_up=0, mysql_up=0, paused=1)
    prom.alerts = [{"state": "firing", "activeAt": "2026-01-02T03:04:05Z",
                    "labels": {"instance": "VM-WEB01", "target": "https://192.0.2.10/health"}}]
    await refresh(hass, entry)

    assert hass.states.get(status_id).state == "teilweise"
    check = hass.states.get(web_check)
    assert check.state == "off"
    assert check.attributes["grund"] == "HTTP-Fehler"
    assert check.attributes["seit"] == "2026-01-02T03:04:05+00:00"
    assert hass.states.get(hv_status).state == "ausgefallen"
    db_ok = hass.states.get(ok_id)
    assert db_ok.state == "4"
    assert db_ok.attributes["gestoert"] == ["MariaDB", "ProxySQL"]

    prom.results = base_results()
    prom.alerts = []
    await refresh(hass, entry)
    assert hass.states.get(status_id).state == "ok"
    assert hass.states.get(web_check).state == "on"
    assert "grund" not in hass.states.get(web_check).attributes


async def test_new_host_and_new_check_appear(hass: HomeAssistant, prom) -> None:
    entry = await setup_entry(hass)
    prom.results["checks"].append(probe("NEW-SW01", "switch", "icmp", "192.0.2.40", 1, site="lab2"))
    prom.results["checks"].append(probe("VM-WEB01", "ssh", "tcp_connect", "192.0.2.10:22", 0, site="lab"))
    await refresh(hass, entry)

    dev = get_device(hass, "NEW-SW01")
    assert dev is not None and dev.model == "Switch"
    assert hass.states.get(entity_id(hass, entry, "sensor", "NEW-SW01", "status")).state == "ok"
    ssh = entity_id(hass, entry, "binary_sensor", "VM-WEB01", "check_ssh_tcp_connect_192_0_2_10_22")
    assert hass.states.get(ssh).state == "off"
    assert hass.states.get(ssh).attributes["grund"] == "Port antwortet nicht"


async def test_vanished_host_unavailable_then_removed(hass: HomeAssistant, prom, freezer) -> None:
    entry = await setup_entry(hass, options={CONF_STALE_HOURS: 1})
    status_id = entity_id(hass, entry, "sensor", "VM-DNS01", "status")

    prom.results["checks"] = [c for c in prom.results["checks"] if c["metric"]["instance"] != "VM-DNS01"]
    await refresh(hass, entry)
    assert hass.states.get(status_id).state == STATE_UNAVAILABLE
    assert get_device(hass, "VM-DNS01") is not None

    freezer.tick(timedelta(hours=2))
    await refresh(hass, entry)
    assert get_device(hass, "VM-DNS01") is None
    assert er.async_get(hass).async_get(status_id) is None

    # The host comes back: device and entities are created again.
    prom.results = base_results()
    await refresh(hass, entry)
    assert get_device(hass, "VM-DNS01") is not None
    new_status = entity_id(hass, entry, "sensor", "VM-DNS01", "status")
    assert hass.states.get(new_status).state == "ok"


async def test_overview_device(hass: HomeAssistant, prom) -> None:
    entry = await setup_entry(
        hass,
        data={CONF_URL: URL, CONF_USERNAME: "u", CONF_PASSWORD: "p",
              CONF_ALERTMANAGER_URL: "https://alertmanager.example.test"},
    )
    assert get_device(hass, OVERVIEW_KEY).name == "Monitoring"
    watchdog = entity_id(hass, entry, "binary_sensor", OVERVIEW_KEY, "watchdog_missing")
    open_id = entity_id(hass, entry, "sensor", OVERVIEW_KEY, "alerts_open")
    cert_id = entity_id(hass, entry, "sensor", OVERVIEW_KEY, "cert_min_days")
    assert hass.states.get(watchdog).state == "on"
    assert hass.states.get(open_id).state == "0"
    assert float(hass.states.get(cert_id).state) == 12.4
    assert hass.states.get(cert_id).attributes["certs_expiring"] == ["dns VM-DNS01 12 d"]

    prom.am_alerts = AM_ALERTS
    await refresh(hass, entry)
    assert hass.states.get(watchdog).state == "off"
    assert hass.states.get(open_id).state == "2"
    assert hass.states.get(open_id).attributes["alert_names"] == ["DiskFull", "HostDown"]

    # Hosts never remove the overview device, even when Prometheus returns nothing.
    prom.results = {}
    await refresh(hass, entry)
    assert get_device(hass, OVERVIEW_KEY) is not None


async def test_unifi_values_on_host_device(hass: HomeAssistant, prom) -> None:
    """A UniFi device with the same name gets its values mirrored onto the host device."""
    unifi_entry = MockConfigEntry(domain="unifi_controller", title="UniFi")
    unifi_entry.add_to_hass(hass)
    mac = "02:00:00:00:00:50"
    unifi_dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=unifi_entry.entry_id,
        identifiers={("unifi_controller", f"{unifi_entry.entry_id}_{mac}")},
        name="LAB SW-LAB01",
    )
    entreg = er.async_get(hass)
    ids = {}
    for key in ("firmware", "clients", "ip"):
        ent = entreg.async_get_or_create(
            "sensor", "unifi_controller", f"{unifi_entry.entry_id}_{mac}_{key}",
            config_entry=unifi_entry, device_id=unifi_dev.id, suggested_object_id=f"unifi_lab_{key}",
        )
        ids[key] = ent.entity_id
    hass.states.async_set(ids["firmware"], "1.2.3")
    hass.states.async_set(ids["clients"], "7")
    hass.states.async_set(ids["ip"], "192.0.2.50")

    prom.results["checks"].append(probe("SW-LAB01", "switch", "icmp", "192.0.2.50", 1, site="lab"))
    prom.results["checks"].append(probe("SW-LAB02", "switch", "icmp", "192.0.2.51", 1, site="lab"))
    entry = await setup_entry(hass)

    fw = entity_id(hass, entry, "sensor", "SW-LAB01", "unifi_controller_firmware")
    clients = entity_id(hass, entry, "sensor", "SW-LAB01", "unifi_controller_clients")
    assert hass.states.get(fw).state == "1.2.3"
    assert hass.states.get(clients).state == "7"
    # Only keys the UniFi device has; no UniFi entities for hosts without a UniFi device.
    assert entity_id(hass, entry, "sensor", "SW-LAB01", "unifi_controller_cpu") is None
    assert entity_id(hass, entry, "sensor", "SW-LAB02", "unifi_controller_firmware") is None
    assert get_device(hass, "SW-LAB01").configuration_url == (
        f"homeassistant://config/devices/device/{unifi_dev.id}"
    )

    # Changed UniFi values show up at once, without waiting for the next Prometheus poll.
    hass.states.async_set(ids["clients"], "12")
    hass.states.async_set(ids["firmware"], "1.2.4")
    await hass.async_block_till_done()
    assert hass.states.get(clients).state == "12"
    assert hass.states.get(fw).state == "1.2.4"

    hass.states.async_set(ids["clients"], "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get(clients).state == STATE_UNAVAILABLE


async def test_unifi_match_by_ip(hass: HomeAssistant, prom) -> None:
    unifi_entry = MockConfigEntry(domain="unifi_controller", title="UniFi")
    unifi_entry.add_to_hass(hass)
    mac = "02:00:00:00:00:60"
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=unifi_entry.entry_id,
        identifiers={("unifi_controller", f"{unifi_entry.entry_id}_{mac}")},
        name="Other name",
    )
    entreg = er.async_get(hass)
    for key, value in (("ip", "192.0.2.20"), ("firmware", "9.9")):
        ent = entreg.async_get_or_create(
            "sensor", "unifi_controller", f"{unifi_entry.entry_id}_{mac}_{key}",
            config_entry=unifi_entry, device_id=dev.id,
        )
        hass.states.async_set(ent.entity_id, value)

    entry = await setup_entry(hass)
    # VM-DNS01 has IP 192.0.2.20 from its dns check target.
    fw = entity_id(hass, entry, "sensor", "VM-DNS01", "unifi_controller_firmware")
    assert hass.states.get(fw).state == "9.9"


async def test_vm_linked_to_hypervisor_via_device_id(hass: HomeAssistant, prom) -> None:
    await setup_entry(hass)
    assert get_device(hass, "VM-WEB01").via_device_id == get_device(hass, "LABHV01").id


async def test_apc_ups_values_on_host_device(hass: HomeAssistant, prom) -> None:
    """An APC UPS entry with another title is matched by its host IP; values follow changes."""
    apc_entry = MockConfigEntry(domain="apc_modbus", title="LAB-APC-OTHER", data={"host": "192.0.2.60"})
    apc_entry.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=apc_entry.entry_id, identifiers={("apc_modbus", apc_entry.entry_id)}, name="LAB-APC-OTHER",
    )
    entreg = er.async_get(hass)
    ids = {}
    for domain, key in (("sensor", "battery_state_of_charge"), ("binary_sensor", "ups_on_battery")):
        ent = entreg.async_get_or_create(
            domain, "apc_modbus", f"apc_modbus_{apc_entry.entry_id}_{key}", config_entry=apc_entry, device_id=dev.id,
        )
        ids[key] = ent.entity_id
    hass.states.async_set(ids["battery_state_of_charge"], "100.0", {"unit_of_measurement": "%"})
    hass.states.async_set(ids["ups_on_battery"], "off")

    prom.results["checks"].append(
        probe("USV-LAB01", "rolle:usv", "icmp", "192.0.2.60", 1, site="lab", rolle="usv-srv")
    )
    entry = await setup_entry(hass)

    charge = entity_id(hass, entry, "sensor", "USV-LAB01", "apc_modbus_battery_state_of_charge")
    on_batt = entity_id(hass, entry, "binary_sensor", "USV-LAB01", "apc_modbus_ups_on_battery")
    assert hass.states.get(charge).state == "100.0"
    assert hass.states.get(charge).attributes["unit_of_measurement"] == "%"
    assert hass.states.get(on_batt).state == "off"
    assert get_device(hass, "USV-LAB01").model == "USV"

    # Power cut: the UPS switches to battery and discharges.
    hass.states.async_set(ids["ups_on_battery"], "on")
    hass.states.async_set(ids["battery_state_of_charge"], "87.5", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert hass.states.get(on_batt).state == "on"
    assert hass.states.get(charge).state == "87.5"


async def test_options_flow_changes_interval_and_links(hass: HomeAssistant, prom) -> None:
    unifi_entry = MockConfigEntry(domain="unifi_controller", title="UniFi")
    unifi_entry.add_to_hass(hass)
    mac = "02:00:00:00:00:70"
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=unifi_entry.entry_id,
        identifiers={("unifi_controller", f"{unifi_entry.entry_id}_{mac}")},
        name="LABHV01",
    )
    ent = er.async_get(hass).async_get_or_create(
        "sensor", "unifi_controller", f"{unifi_entry.entry_id}_{mac}_firmware",
        config_entry=unifi_entry, device_id=dev.id,
    )
    hass.states.async_set(ent.entity_id, "4.5.6")
    entry = await setup_entry(hass)
    fw = entity_id(hass, entry, "sensor", "LABHV01", "unifi_controller_firmware")
    assert hass.states.get(fw).state == "4.5.6"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"scan_interval": 120, "stale_hours": 6, "link_unifi": False, "link_apc": True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.runtime_data.update_interval.total_seconds() == 120
    assert hass.states.get(fw).state == STATE_UNAVAILABLE


async def test_reconfigure_keeps_password_when_empty(hass: HomeAssistant, prom) -> None:
    entry = await setup_entry(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "https://prometheus2.example.test/", CONF_USERNAME: "user2", CONF_PASSWORD: "",
         CONF_ALERTMANAGER_URL: "https://alertmanager.example.test/"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_URL] == "https://prometheus2.example.test"
    assert entry.data[CONF_USERNAME] == "user2"
    assert entry.data[CONF_PASSWORD] == "pw"
    assert entry.data[CONF_ALERTMANAGER_URL] == "https://alertmanager.example.test"
    assert entry.unique_id == "https://prometheus2.example.test"
