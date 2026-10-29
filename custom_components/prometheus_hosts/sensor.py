"""Host status, check summary and metrics as sensors."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import STATUS_OPTIONS
from .coordinator import PrometheusHostsConfigEntry, PrometheusHostsCoordinator
from .entity import HostEntity, setup_dynamic
from .parse import Host

# metric key -> (name, unit, icon)
METRICS = {
    "response_time": ("Antwortzeit", UnitOfTime.MILLISECONDS, "mdi:timer-sand"),
    "cpu": ("CPU", PERCENTAGE, "mdi:cpu-64-bit"),
    "ram": ("RAM", PERCENTAGE, "mdi:memory"),
    "disk": ("Disk", PERCENTAGE, "mdi:harddisk"),
    "galera_cluster_size": ("Galera-Knoten", None, "mdi:database-sync"),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: PrometheusHostsConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    def factory(coordinator: PrometheusHostsCoordinator, host: Host):
        yield "status", StatusSensor(coordinator, host.key)
        yield "checks_ok", ChecksOkSensor(coordinator, host.key)
        for metric in host.metrics:
            if metric in METRICS:
                yield metric, MetricSensor(coordinator, host.key, metric)

    setup_dynamic(entry, async_add_entities, factory)


class StatusSensor(HostEntity, SensorEntity):
    """ok / teilweise / ausgefallen; host details as attributes."""

    _attr_name = "Status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS
    _unrecorded_attributes = frozenset({"standort", "art", "ip", "hyperv_host", "mandant"})

    def __init__(self, coordinator: PrometheusHostsCoordinator, host_key: str) -> None:
        super().__init__(coordinator, host_key, "status")

    @property
    def native_value(self) -> str | None:
        return self.host.status if self.host else None

    @property
    def icon(self) -> str:
        return {
            "ok": "mdi:check-circle",
            "teilweise": "mdi:alert-circle",
            "ausgefallen": "mdi:close-circle",
        }.get(self.native_value or "", "mdi:help-circle")

    @property
    def extra_state_attributes(self) -> dict:
        h = self.host
        if h is None:
            return {}
        return {
            "standort": h.site,
            "art": h.kind,
            "ip": h.ip,
            "hyperv_host": h.hyperv,
            "mandant": h.tenant,
        }


class ChecksOkSensor(HostEntity, SensorEntity):
    """Number of passing checks; total and failing services as attributes."""

    _attr_name = "Pruefungen ok"
    _attr_icon = "mdi:format-list-checks"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"gestoert"})

    def __init__(self, coordinator: PrometheusHostsCoordinator, host_key: str) -> None:
        super().__init__(coordinator, host_key, "checks_ok")

    @property
    def native_value(self) -> int | None:
        return self.host.checks_ok if self.host else None

    @property
    def extra_state_attributes(self) -> dict:
        h = self.host
        if h is None:
            return {}
        failing = sorted({c.service for c in h.checks.values() if not c.ok})
        return {"gesamt": len(h.checks), "gestoert": failing}


class MetricSensor(HostEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: PrometheusHostsCoordinator, host_key: str, metric: str) -> None:
        super().__init__(coordinator, host_key, metric)
        self.metric = metric
        name, unit, icon = METRICS[metric]
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        if unit == UnitOfTime.MILLISECONDS:
            self._attr_device_class = SensorDeviceClass.DURATION

    @property
    def available(self) -> bool:
        return super().available and self.metric in self.host.metrics

    @property
    def native_value(self) -> float | None:
        return self.host.metrics.get(self.metric) if self.host else None
