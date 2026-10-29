"""Base entity and dynamic entity creation for host devices."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, OVERVIEW_KEY
from .coordinator import PrometheusHostsConfigEntry, PrometheusHostsCoordinator
from .links import MIRRORS, MirrorSpec
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


def mirror_factory(platform: str, cls):
    """Factory for setup_dynamic: one mirror entity per linked device value of this platform."""

    def factory(coordinator: PrometheusHostsCoordinator, host: Host):
        links = coordinator.data.links.get(host.key, []) if coordinator.data else []
        for dev in links:
            for spec in MIRRORS.get(dev.domain, ()):
                if spec.platform == platform and spec.key in dev.entities:
                    yield f"{dev.domain}_{spec.key}", cls(coordinator, host.key, dev.domain, spec)

    return factory


class MirrorEntity(HostEntity):
    """Shows a value of a linked device (UniFi, APC UPS, ...) on the host device."""

    def __init__(
        self, coordinator: PrometheusHostsCoordinator, host_key: str, domain: str, spec: MirrorSpec
    ) -> None:
        super().__init__(coordinator, host_key, f"{domain}_{spec.key}")
        self.linked_domain = domain
        self.spec = spec
        self._attr_name = spec.name
        self._attr_device_class = spec.device_class
        self._unsub_source = None
        self._tracked: str | None = None

    @property
    def source(self) -> str | None:
        data = self.coordinator.data
        for dev in data.links.get(self.host_key, []) if data else []:
            if dev.domain == self.linked_domain:
                return dev.entities.get(self.spec.key)
        return None

    @property
    def source_state(self):
        source = self.source
        return self.hass.states.get(source) if source else None

    @property
    def available(self) -> bool:
        state = self.source_state
        return super().available and state is not None and state.state not in ("unavailable", "unknown")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._track_source()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_source:
            self._unsub_source()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._track_source()
        super()._handle_coordinator_update()

    @callback
    def _track_source(self) -> None:
        """Follow the source entity, so changes show up without waiting for the next poll."""
        source = self.source
        if source == self._tracked:
            return
        if self._unsub_source:
            self._unsub_source()
            self._unsub_source = None
        self._tracked = source
        if source:
            self._unsub_source = async_track_state_change_event(self.hass, source, self._source_changed)

    @callback
    def _source_changed(self, event: Event[EventStateChangedData]) -> None:
        self.async_write_ha_state()
