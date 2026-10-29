"""Prometheus Hosts: one device per monitored host."""

from __future__ import annotations

from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PrometheusClient
from .const import CONF_VERIFY_SSL, DOMAIN, OVERVIEW_KEY
from .coordinator import PrometheusHostsConfigEntry, PrometheusHostsCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: PrometheusHostsConfigEntry) -> bool:
    session = async_get_clientsession(hass, verify_ssl=entry.data.get(CONF_VERIFY_SSL, True))
    client = PrometheusClient(
        session, entry.data[CONF_URL], entry.data.get(CONF_USERNAME), entry.data.get(CONF_PASSWORD)
    )
    coordinator = PrometheusHostsCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: PrometheusHostsConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PrometheusHostsConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: PrometheusHostsConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a device in the UI once Prometheus no longer reports the host."""
    data = entry.runtime_data.data
    hosts = data.hosts if data else {}
    return not any(
        d == DOMAIN and (k in hosts or k == OVERVIEW_KEY) for d, k in device.identifiers
    )
