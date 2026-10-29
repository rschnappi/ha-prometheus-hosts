"""Find the UniFi Controller (unifi_controller) device that belongs to a Prometheus host.

The UniFi integration registers one device per network device with entities whose unique_id is
"<entry_id>_<mac>_<key>". Since Home Assistant 2026.8 devices of different integrations are no
longer merged, so the host device shows the UniFi values itself (see sensor.UnifiSensor).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .parse import Host

UNIFI_DOMAIN = "unifi_controller"
_UNIQUE = re.compile(r"_([0-9a-f]{2}(?::[0-9a-f]{2}){5})_([a-z_]+)$")


@dataclass
class UnifiDevice:
    device_id: str
    name: str
    entities: dict[str, str] = field(default_factory=dict)  # key -> entity_id


def collect_unifi_devices(hass: HomeAssistant) -> list[UnifiDevice]:
    """All UniFi network devices with their entities, keyed by the UniFi sensor key."""
    entries = hass.config_entries.async_entries(UNIFI_DOMAIN)
    if not entries:
        return []
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    devices: dict[str, UnifiDevice] = {}
    for entry in entries:
        for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            m = _UNIQUE.search(ent.unique_id or "")
            if not m or not ent.device_id or ent.domain != "sensor":
                continue
            dev = devices.get(ent.device_id)
            if dev is None:
                device = dev_reg.async_get(ent.device_id)
                name = (device.name_by_user or device.name or "") if device else ""
                dev = devices[ent.device_id] = UnifiDevice(ent.device_id, name)
            dev.entities[m.group(2)] = ent.entity_id
    return list(devices.values())


def match_hosts(
    hass: HomeAssistant, hosts: dict[str, Host], devices: list[UnifiDevice]
) -> dict[str, UnifiDevice]:
    """Host key -> UniFi device, matched by name (the UniFi name may carry a prefix), else by IP."""
    by_name: dict[str, UnifiDevice] = {}
    by_ip: dict[str, UnifiDevice] = {}
    for dev in devices:
        if dev.name:
            by_name.setdefault(dev.name.upper(), dev)
            by_name.setdefault(dev.name.split(" ")[-1].upper(), dev)
        if (ip_entity := dev.entities.get("ip")) and (state := hass.states.get(ip_entity)):
            by_ip.setdefault(state.state, dev)
    out: dict[str, UnifiDevice] = {}
    for key, host in hosts.items():
        dev = by_name.get(key) or (by_ip.get(host.ip) if host.ip else None)
        if dev:
            out[key] = dev
    return out
