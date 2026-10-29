"""Host status, check summary and metrics as sensors."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import STATUS_OPTIONS
from .coordinator import PrometheusHostsConfigEntry, PrometheusHostsCoordinator
from .entity import HostEntity, MirrorEntity, OverviewEntity, mirror_factory, setup_dynamic
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
    setup_dynamic(entry, async_add_entities, mirror_factory("sensor", MirrorSensor))
    coordinator = entry.runtime_data
    async_add_entities(OverviewSensor(coordinator, key) for key in OVERVIEW_SENSORS)
    async_add_entities(OverviewTextSensor(coordinator, key) for key in OVERVIEW_TEXT)


# overview key -> (name, unit, icon, attribute with details)
OVERVIEW_SENSORS = {
    "hosts_total": ("Hosts gesamt", None, "mdi:server-network", None),
    "hosts_down": ("Hosts ausgefallen", None, "mdi:server-off", "hosts_down_names"),
    "hosts_partial": ("Hosts teilweise gestoert", None, "mdi:server-minus", "hosts_partial_names"),
    "checks_total": ("Pruefungen gesamt", None, "mdi:format-list-checks", None),
    "alerts_open": ("Alarme offen", None, "mdi:bell-alert", "alert_names"),
    "alerts_critical": ("Alarme kritisch", None, "mdi:alert-octagon", None),
    "alerts_warning": ("Alarme Warnung", None, "mdi:alert", None),
    "targets_down": ("Ziele down", None, "mdi:target", None),
    "nodes_not_ready": ("Knoten nicht bereit", None, "mdi:server-off", None),
    "pods_failed": ("Pods fehlerhaft", None, "mdi:kubernetes", None),
    "probes_failed": ("Pruefungen fehlgeschlagen", None, "mdi:lan-disconnect", None),
    "cert_min_days": ("Zertifikat Ablauf naechstes", UnitOfTime.DAYS, "mdi:certificate", "certs_expiring"),
    "certs_checked": ("Zertifikate geprueft", None, "mdi:certificate-outline", None),
}


class OverviewSensor(OverviewEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset(
        {"alert_names", "certs_expiring", "hosts_down_names", "hosts_partial_names"}
    )

    def __init__(self, coordinator: PrometheusHostsCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        name, unit, icon, self._detail = OVERVIEW_SENSORS[key]
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon

    @property
    def native_value(self):
        data = self.coordinator.data
        return getattr(data.overview, self.key) if data else None

    @property
    def extra_state_attributes(self) -> dict | None:
        data = self.coordinator.data
        if not self._detail or not data:
            return None
        return {self._detail: getattr(data.overview, self._detail)}


# overview key -> (name, icon, list attribute, text when the list is empty, table attribute)
OVERVIEW_TEXT = {
    "alert_list": ("Alarmliste", "mdi:format-list-bulleted", "alert_names", "keine", None),
    "cert_list": (
        "Zertifikate Ablaufliste",
        "mdi:format-list-bulleted",
        "certs_expiring",
        "keine unter 30 Tagen",
        "certs_next",
    ),
}


class OverviewTextSensor(OverviewEntity, SensorEntity):
    """A list from the overview as comma-separated text (max. 250 characters)."""

    _unrecorded_attributes = frozenset({"naechste"})

    def __init__(self, coordinator: PrometheusHostsCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        name, icon, self._source, self._empty, self._table = OVERVIEW_TEXT[key]
        self._attr_name = name
        self._attr_icon = icon

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if not data:
            return None
        items = getattr(data.overview, self._source)
        return ", ".join(items)[:250] if items else self._empty

    @property
    def extra_state_attributes(self) -> dict | None:
        data = self.coordinator.data
        if not self._table or not data:
            return None
        return {"naechste": getattr(data.overview, self._table)}


class MirrorSensor(MirrorEntity, SensorEntity):
    def __init__(self, coordinator, host_key, domain, spec) -> None:
        super().__init__(coordinator, host_key, domain, spec)
        self._attr_state_class = spec.state_class

    @property
    def native_value(self):
        state = self.source_state
        if state is None:
            return None
        if self.spec.device_class == SensorDeviceClass.TIMESTAMP:
            return dt_util.parse_datetime(state.state)
        return state.state

    @property
    def native_unit_of_measurement(self) -> str | None:
        state = self.source_state
        return state.attributes.get("unit_of_measurement") if state else None


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
