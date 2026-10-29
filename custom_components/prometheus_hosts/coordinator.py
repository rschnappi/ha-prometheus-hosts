"""Polls Prometheus and keeps the host devices in sync."""

from __future__ import annotations

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
    CONF_SCAN_INTERVAL,
    CONF_STALE_HOURS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_HOURS,
    DOMAIN,
    QUERIES,
)
from .parse import Host, build_hosts

_LOGGER = logging.getLogger(__name__)

type PrometheusHostsConfigEntry = ConfigEntry[PrometheusHostsCoordinator]


class PrometheusHostsCoordinator(DataUpdateCoordinator[dict[str, Host]]):
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
        # Host keys whose device was removed in the last update; platforms forget them.
        self.removed: set[str] = set()

    async def _async_update_data(self) -> dict[str, Host]:
        try:
            results = await self.client.query_all(QUERIES)
            try:
                alerts = await self.client.alerts()
            except PrometheusError:
                alerts = []
        except PrometheusAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except PrometheusError as err:
            raise UpdateFailed(str(err)) from err

        hosts = build_hosts(results, alerts)
        self._sync_devices(hosts)
        return hosts

    def _sync_devices(self, hosts: dict[str, Host]) -> None:
        """Create/update one device per host, hypervisors first, and drop stale ones."""
        reg = dr.async_get(self.hass)
        now = dt_util.utcnow().timestamp()
        entry_id = self.config_entry.entry_id

        if not self._last_seen:
            # After a restart, devices that no longer exist start their grace period now.
            for device in dr.async_entries_for_config_entry(reg, entry_id):
                for domain, key in device.identifiers:
                    if domain == DOMAIN:
                        self._last_seen[key] = now

        hypervisors = {h.hyperv for h in hosts.values() if h.hyperv}
        for h in sorted(hosts.values(), key=lambda h: (h.key not in hypervisors, h.key)):
            self._last_seen[h.key] = now
            via = h.hyperv if h.hyperv in hosts and h.hyperv != h.key else None
            sig = (h.name, h.kind, h.site, via)
            if self._device_sig.get(h.key) == sig:
                continue
            self._device_sig[h.key] = sig
            reg.async_get_or_create(
                config_entry_id=entry_id,
                identifiers={(DOMAIN, h.key)},
                name=h.name,
                manufacturer="Prometheus",
                model=h.kind,
                suggested_area=h.site or None,
                via_device=(DOMAIN, via) if via else None,
            )

        self.removed = set()
        stale_after = self.config_entry.options.get(CONF_STALE_HOURS, DEFAULT_STALE_HOURS) * 3600
        for key, seen in list(self._last_seen.items()):
            if key in hosts or now - seen < stale_after:
                continue
            device = reg.async_get_device(identifiers={(DOMAIN, key)})
            if device:
                reg.async_update_device(device.id, remove_config_entry_id=entry_id)
            del self._last_seen[key]
            self._device_sig.pop(key, None)
            self.removed.add(key)
