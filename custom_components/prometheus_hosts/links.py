"""Find devices of other integrations that belong to a Prometheus host and mirror their values.

Since Home Assistant 2026.8 devices of different integrations are no longer merged into one
device, so the host device shows selected values of the matching device itself.

Supported sources:
- UniFi Controller (unifi_controller): entity unique_id "<entry_id>_<mac>_<key>", one device per
  network device; matched by device name (a name prefix is ignored), else by the "ip" sensor.
- APC UPS Modbus (apc_modbus): entity unique_id "apc_modbus_<entry_id>_<key>", one config entry
  per UPS; matched by entry title, else by the host in the entry data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .parse import Host


@dataclass(frozen=True)
class MirrorSpec:
    key: str
    name: str
    platform: str = "sensor"
    device_class: str | None = None
    state_class: str | None = None


@dataclass
class LinkedDevice:
    domain: str
    device_id: str
    name: str
    ip: str = ""
    entities: dict[str, str] = field(default_factory=dict)  # key -> entity_id


UNIFI = "unifi_controller"
APC = "apc_modbus"

MIRRORS: dict[str, tuple[MirrorSpec, ...]] = {
    UNIFI: (
        MirrorSpec("status", "UniFi Status"),
        MirrorSpec("firmware", "UniFi Firmware"),
        MirrorSpec("uptime", "UniFi Gestartet", device_class=SensorDeviceClass.TIMESTAMP),
        MirrorSpec("clients", "UniFi Clients", state_class=SensorStateClass.MEASUREMENT),
        MirrorSpec("cpu", "UniFi CPU", state_class=SensorStateClass.MEASUREMENT),
        MirrorSpec("mem", "UniFi Speicher", state_class=SensorStateClass.MEASUREMENT),
        MirrorSpec(
            "temperature", "UniFi Temperatur",
            device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT,
        ),
    ),
    APC: (
        MirrorSpec(
            "battery_state_of_charge", "USV Ladung",
            device_class=SensorDeviceClass.BATTERY, state_class=SensorStateClass.MEASUREMENT,
        ),
        MirrorSpec(
            "runtime_remaining", "USV Restlaufzeit",
            device_class=SensorDeviceClass.DURATION, state_class=SensorStateClass.MEASUREMENT,
        ),
        MirrorSpec("output_load", "USV Last", state_class=SensorStateClass.MEASUREMENT),
        MirrorSpec(
            "battery_temperature", "USV Batterietemperatur",
            device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT,
        ),
        MirrorSpec(
            "input_voltage", "USV Eingangsspannung",
            device_class=SensorDeviceClass.VOLTAGE, state_class=SensorStateClass.MEASUREMENT,
        ),
        MirrorSpec("last_self_test_result", "USV Letzter Selbsttest"),
        MirrorSpec("ups_online", "USV Online", platform="binary_sensor"),
        MirrorSpec("ups_on_battery", "USV Auf Batterie", platform="binary_sensor", device_class="problem"),
        MirrorSpec(
            "battery_needs_replacement", "USV Batterie tauschen",
            platform="binary_sensor", device_class="problem",
        ),
    ),
}

_UNIFI_UNIQUE = re.compile(r"_([0-9a-f]{2}(?::[0-9a-f]{2}){5})_([a-z0-9_]+)$")


def _host_only(value: str) -> str:
    v = re.sub(r"^[a-z]+://", "", value or "")
    return v.split("/")[0].split(":")[0]


def _collect_unifi(hass: HomeAssistant) -> list[LinkedDevice]:
    ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
    devices: dict[str, LinkedDevice] = {}
    for entry in hass.config_entries.async_entries(UNIFI):
        for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            m = _UNIFI_UNIQUE.search(ent.unique_id or "")
            if not m or not ent.device_id or ent.domain != "sensor":
                continue
            dev = devices.get(ent.device_id)
            if dev is None:
                device = dev_reg.async_get(ent.device_id)
                name = (device.name_by_user or device.name or "") if device else ""
                dev = devices[ent.device_id] = LinkedDevice(UNIFI, ent.device_id, name)
            dev.entities[m.group(2)] = ent.entity_id
    for dev in devices.values():
        if (ip := dev.entities.get("ip")) and (state := hass.states.get(ip)):
            dev.ip = state.state
    return list(devices.values())


def _collect_apc(hass: HomeAssistant) -> list[LinkedDevice]:
    ent_reg = er.async_get(hass)
    out = []
    for entry in hass.config_entries.async_entries(APC):
        prefix = f"{APC}_{entry.entry_id}_"
        dev: LinkedDevice | None = None
        for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            uid = ent.unique_id or ""
            if not uid.startswith(prefix) or not ent.device_id:
                continue
            if dev is None:
                ip = _host_only(str(entry.data.get("host") or entry.data.get("ip_address") or ""))
                dev = LinkedDevice(APC, ent.device_id, entry.title or "", ip)
            dev.entities[uid[len(prefix):]] = ent.entity_id
        if dev:
            out.append(dev)
    return out


def find_links(hass: HomeAssistant, hosts: dict[str, Host]) -> dict[str, list[LinkedDevice]]:
    """Host key -> matching devices of the supported integrations."""
    out: dict[str, list[LinkedDevice]] = {}
    for devices in (_collect_unifi(hass), _collect_apc(hass)):
        by_name: dict[str, LinkedDevice] = {}
        by_ip: dict[str, LinkedDevice] = {}
        for dev in devices:
            if dev.name:
                by_name.setdefault(dev.name.upper(), dev)
                by_name.setdefault(dev.name.split(" ")[-1].upper(), dev)
            if dev.ip:
                by_ip.setdefault(dev.ip, dev)
        for key, host in hosts.items():
            dev = by_name.get(key) or (by_ip.get(host.ip) if host.ip else None)
            if dev:
                out.setdefault(key, []).append(dev)
    return out
