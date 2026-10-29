"""One connectivity binary sensor per check (on = check ok)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PrometheusHostsConfigEntry, PrometheusHostsCoordinator
from .entity import HostEntity, MirrorEntity, OverviewEntity, mirror_factory, setup_dynamic
from .parse import Check, Host


async def async_setup_entry(
    hass: HomeAssistant, entry: PrometheusHostsConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    def factory(coordinator: PrometheusHostsCoordinator, host: Host):
        for check in host.checks.values():
            yield check.key, CheckBinarySensor(coordinator, host.key, check)

    setup_dynamic(entry, async_add_entities, factory)
    setup_dynamic(entry, async_add_entities, mirror_factory("binary_sensor", MirrorBinarySensor))
    async_add_entities([WatchdogBinarySensor(entry.runtime_data)])


class MirrorBinarySensor(MirrorEntity, BinarySensorEntity):
    @property
    def is_on(self) -> bool | None:
        state = self.source_state
        return state.state == "on" if state else None


class WatchdogBinarySensor(OverviewEntity, BinarySensorEntity):
    """On (problem) when the always-firing Watchdog alert is missing: alerting pipeline broken."""

    _attr_name = "Watchdog fehlt"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: PrometheusHostsCoordinator) -> None:
        super().__init__(coordinator, "watchdog_missing")

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        return data.overview.watchdog_missing if data else None


class CheckBinarySensor(HostEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _unrecorded_attributes = frozenset({"dienst", "modul", "ziel"})

    def __init__(self, coordinator: PrometheusHostsCoordinator, host_key: str, check: Check) -> None:
        super().__init__(coordinator, host_key, f"check_{check.key}")
        self.check_key = check.key
        self._attr_name = check.name

    @property
    def check(self) -> Check | None:
        host = self.host
        return host.checks.get(self.check_key) if host else None

    @property
    def available(self) -> bool:
        return super().available and self.check is not None

    @property
    def is_on(self) -> bool | None:
        check = self.check
        return check.ok if check else None

    @property
    def extra_state_attributes(self) -> dict:
        check = self.check
        if check is None:
            return {}
        attrs = {"dienst": check.service, "modul": check.module, "ziel": check.target}
        if not check.ok:
            attrs["grund"] = check.reason
            if check.since:
                attrs["seit"] = check.since.isoformat()
        return attrs
