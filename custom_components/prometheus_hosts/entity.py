"""Base entity and dynamic entity creation for host devices."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, OVERVIEW_KEY
from .coordinator import PrometheusHostsConfigEntry, PrometheusHostsCoordinator
from .parse import Host


class HostEntity(CoordinatorEntity[PrometheusHostsCoordinator]):
    """Entity that belongs to one host device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PrometheusHostsCoordinator, host_key: str, suffix: str) -> None:
        super().__init__(coordinator)
        self.host_key = host_key
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{host_key}_{suffix}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, host_key)})

    @property
    def host(self) -> Host | None:
        data = self.coordinator.data
        return data.hosts.get(self.host_key) if data else None

    @property
    def available(self) -> bool:
        return super().available and self.host is not None


def setup_dynamic(
    entry: PrometheusHostsConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[PrometheusHostsCoordinator, Host], Iterable[tuple[str, Entity]]],
) -> None:
    """Add entities for every host now and whenever new hosts or checks appear.

    factory yields (unique key, entity) pairs; known keys are skipped.
    """
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_new() -> None:
        for key in coordinator.removed:
            known.difference_update({k for k in known if k.startswith(f"{key}|")})
        new = []
        hosts = coordinator.data.hosts if coordinator.data else {}
        for host in hosts.values():
            for key, entity in factory(coordinator, host):
                full = f"{host.key}|{key}"
                if full not in known:
                    known.add(full)
                    new.append(entity)
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))


class OverviewEntity(CoordinatorEntity[PrometheusHostsCoordinator]):
    """Entity of the Monitoring overview device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PrometheusHostsCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self.key = key
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{OVERVIEW_KEY}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, OVERVIEW_KEY)})
