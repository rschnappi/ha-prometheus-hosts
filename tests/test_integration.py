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
    for dev in dr.async_get(hass).devices.values():
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
