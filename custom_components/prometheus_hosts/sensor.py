"""Host status, check summary and metrics as sensors."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import STATUS_OPTIONS
from .coordinator import PrometheusHostsConfigEntry, PrometheusHostsCoordinator
from .entity import HostEntity, OverviewEntity, setup_dynamic
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
        unifi_dev = coordinator.data.unifi.get(host.key) if coordinator.data else None
        if unifi_dev:
            for key in UNIFI_SENSORS:
                if key in unifi_dev.entities:
                    yield f"unifi_{key}", UnifiSensor(coordinator, host.key, key)

    setup_dynamic(entry, async_add_entities, factory)
    coordinator = entry.runtime_data
    async_add_entities(OverviewSensor(coordinator, key) for key in OVERVIEW_SENSORS)


# overview key -> (name, unit, icon, attribute with details)
OVERVIEW_SENSORS = {
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
    _unrecorded_attributes = frozenset({"alert_names", "certs_expiring"})

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


# UniFi sensor key -> (name, device class, state class, unit when the source has none)
UNIFI_SENSORS = {
    "status": ("UniFi Status", None, None),
    "firmware": ("UniFi Firmware", None, None),
    "uptime": ("UniFi Gestartet", SensorDeviceClass.TIMESTAMP, None),
    "clients": ("UniFi Clients", None, SensorStateClass.MEASUREMENT),
    "cpu": ("UniFi CPU", None, SensorStateClass.MEASUREMENT),
    "mem": ("UniFi Speicher", None, SensorStateClass.MEASUREMENT),
    "temperature": ("UniFi Temperatur", SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT),
}


class UnifiSensor(HostEntity, SensorEntity):
    """Shows a value of the matching UniFi Controller device on the host device."""

    def __init__(self, coordinator: PrometheusHostsCoordinator, host_key: str, key: str) -> None:
        super().__init__(coordinator, host_key, f"unifi_{key}")
        self.key = key
        name, self._attr_device_class, self._attr_state_class = UNIFI_SENSORS[key]
        self._attr_name = name
        self._unsub_source = None
        self._tracked: str | None = None

    @property
    def source(self) -> str | None:
        data = self.coordinator.data
        dev = data.unifi.get(self.host_key) if data else None
        return dev.entities.get(self.key) if dev else None

    @property
    def available(self) -> bool:
        source = self.source
        state = self.hass.states.get(source) if source else None
        return super().available and state is not None and state.state not in ("unavailable", "unknown")

    @property
    def native_value(self):
        source = self.source
        state = self.hass.states.get(source) if source else None
        if state is None:
            return None
        if self._attr_device_class == SensorDeviceClass.TIMESTAMP:
            return dt_util.parse_datetime(state.state)
        return state.state

    @property
    def native_unit_of_measurement(self) -> str | None:
        source = self.source
        state = self.hass.states.get(source) if source else None
        return state.attributes.get("unit_of_measurement") if state else None

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
        """Follow the UniFi entity directly, so changes show up without waiting for the next poll."""
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
