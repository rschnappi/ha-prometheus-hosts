"""Polls Prometheus and keeps the host devices in sync."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import PrometheusAuthError, PrometheusClient, PrometheusError
from .const import (
    CONF_ALERTMANAGER_URL,
    CONF_SCAN_INTERVAL,
    CONF_STALE_HOURS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_HOURS,
    DOMAIN,
    OVERVIEW_KEY,
    OVERVIEW_NAME,
    QUERIES,
)
from .parse import Host, Overview, build_hosts, build_overview
from .links import LinkedDevice, find_links

_LOGGER = logging.getLogger(__name__)

type PrometheusHostsConfigEntry = ConfigEntry[PrometheusHostsCoordinator]


@dataclass
class Snapshot:
    """Result of one poll."""

    hosts: dict[str, Host] = field(default_factory=dict)
    overview: Overview = field(default_factory=Overview)
    links: dict[str, list[LinkedDevice]] = field(default_factory=dict)


class PrometheusHostsCoordinator(DataUpdateCoordinator[Snapshot]):
    config_entry: PrometheusHostsConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: PrometheusHostsConfigEntry, client: PrometheusClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.client = client
        self._last_seen: dict[str, float] = {}
        self._device_sig: dict[str, tuple] = {}
        self._device_ids: dict[str, str] = {}
        self._linked_count = -1
        # Host keys whose device was removed in the last update; platforms forget them.
        self.removed: set[str] = set()

    async def _async_update_data(self) -> Snapshot:
        am_url = self.config_entry.data.get(CONF_ALERTMANAGER_URL)
        try:
            results = await self.client.query_all(QUERIES)
            try:
                alerts = await self.client.alerts()
            except PrometheusError:
                alerts = []
            am_alerts = alerts
            if am_url:
                try:
                    am_alerts = await self.client.alertmanager_alerts(am_url)
                except PrometheusError as err:
                    _LOGGER.debug("Alertmanager not reachable: %s", err)
        except PrometheusAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except PrometheusError as err:
            raise UpdateFailed(str(err)) from err

        hosts = build_hosts(results, alerts)
        links = find_links(self.hass, hosts)
        if len(links) != self._linked_count:
            self._linked_count = len(links)
            per_domain: dict[str, int] = {}
            for devs in links.values():
                for dev in devs:
                    per_domain[dev.domain] = per_domain.get(dev.domain, 0) + 1
            _LOGGER.info("Linked devices: %d of %d hosts %s", len(links), len(hosts), per_domain)
        self._sync_devices(hosts, links)
        return Snapshot(hosts=hosts, overview=build_overview(results, am_alerts), links=links)

    def _sync_devices(self, hosts: dict[str, Host], links: dict[str, list[LinkedDevice]]) -> None:
        """Create/update one device per host, hypervisors first, and drop stale ones."""
        reg = dr.async_get(self.hass)
        now = dt_util.utcnow().timestamp()
        entry_id = self.config_entry.entry_id

        if not self._last_seen:
            # After a restart, devices that no longer exist start their grace period now.
            for device in dr.async_entries_for_config_entry(reg, entry_id):
                for domain, key in device.identifiers:
                    if domain == DOMAIN and key != OVERVIEW_KEY:
                        self._last_seen[key] = now

        if OVERVIEW_KEY not in self._device_sig:
            self._device_sig[OVERVIEW_KEY] = ()
            reg.async_get_or_create(
                config_entry_id=entry_id,
                identifiers={(DOMAIN, OVERVIEW_KEY)},
                name=OVERVIEW_NAME,
                manufacturer="Prometheus",
                model="Uebersicht",
            )

        hypervisors = {h.hyperv for h in hosts.values() if h.hyperv}
        for h in sorted(hosts.values(), key=lambda h: (h.key not in hypervisors, h.key)):
            self._last_seen[h.key] = now
            via = h.hyperv if h.hyperv in hosts and h.hyperv != h.key else None
            via_id = self._device_ids.get(via) if via else None
            linked = links.get(h.key)
            url = f"homeassistant://config/devices/device/{linked[0].device_id}" if linked else None
            sig = (h.name, h.kind, h.site, via_id, url)
            if self._device_sig.get(h.key) == sig:
                continue
            self._device_sig[h.key] = sig
            device = reg.async_get_or_create(
                config_entry_id=entry_id,
                identifiers={(DOMAIN, h.key)},
                name=h.name,
                manufacturer="Prometheus",
                model=h.kind,
                suggested_area=h.site or None,
                configuration_url=url,
            )
            self._device_ids[h.key] = device.id
            if device.via_device_id != via_id:
                reg.async_update_device(device.id, via_device_id=via_id)

        self.removed = set()
        stale_after = self.config_entry.options.get(CONF_STALE_HOURS, DEFAULT_STALE_HOURS) * 3600
        stale = {k for k, seen in self._last_seen.items() if k not in hosts and now - seen >= stale_after}
        if not stale:
            return
        for device in dr.async_entries_for_config_entry(reg, entry_id):
            if any(d == DOMAIN and k in stale for d, k in device.identifiers):
                reg.async_update_device(device.id, remove_config_entry_id=entry_id)
        for key in stale:
            del self._last_seen[key]
            self._device_sig.pop(key, None)
            self._device_ids.pop(key, None)
            self.removed.add(key)
