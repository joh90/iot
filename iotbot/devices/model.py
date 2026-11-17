"""Rooms and devices loaded from devices.json + commands.json.

Features are data-driven: an IR device has exactly the features whose codes were
captured in commands.json (plus a few known macros). Nothing else can be called,
which replaces the old getattr-on-anything dispatch (B2) and keeps buttons in
sync with real codes (B17).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Literal

logger = logging.getLogger(__name__)


class DeviceType(IntEnum):
    AIRCON = 1
    TV = 2
    SET_TOP_BOX = 3
    PROJECTOR = 4
    AMPLIFIER = 5


# Broadlink devices controlled directly over the network (not via IR)
PLUG_TYPES = ("SP2", "SP4")

# Broadlink packet first byte: 0x26 IR, 0xb2 RF 433MHz, 0xd7 RF 315MHz
PACKET_TYPES = (0x26, 0xB2, 0xD7)

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
FEATURE_RE = re.compile(r"^[a-z0-9_]{1,24}$")

# Shown first, in this order; everything else follows commands.json order
FEATURE_ORDER = ("power_on", "power_off")

# Words the old bot accepted that map onto another captured key when the exact
# one was never captured (mute is a toggle on most remotes)
ALIASES = {"unmute": "mute", "change_input": "input", "input": "change_input"}


@dataclass(frozen=True, slots=True)
class Feature:
    key: str
    label: str
    # IR: one or more packets sent in order, `gap_s` apart. Plugs: empty.
    codes: tuple[bytes, ...] = ()
    gap_s: float = 0.0
    # Safe to resend if we are unsure the first send landed. True only for
    # full-state AC frames; a TV power toggle sent twice undoes itself.
    idempotent: bool = False
    # Plug operations are not IR codes
    plug_op: str | None = None


@dataclass(slots=True)
class Device:
    id: str
    room: str
    kind: Literal["ir", "plug"]
    type_name: str               # "aircon", "tv", ... or "sp2"/"sp4"
    features: dict[str, Feature]
    brand: str = ""
    model: str = ""
    device_type: DeviceType | None = None
    mac: str = ""                # plugs only, normalized
    bl_type: str = ""            # plugs only, e.g. "SP2"

    @property
    def label(self) -> str:
        return self.id


@dataclass(slots=True)
class Room:
    name: str
    rm_mac: str = ""             # normalized, "" if the room has no RM
    rm_type: str = ""
    rm_ip: str = ""              # optional fixed IP (DHCP reservation)
    devices: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Registry:
    rooms: dict[str, Room] = field(default_factory=dict)
    devices: dict[str, Device] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def room_of(self, device_id: str) -> Room | None:
        d = self.devices.get(device_id)
        return self.rooms.get(d.room) if d else None


def normalize_mac(mac: str) -> str:
    """'78:0F:77:1A:BC:DE' / '780f771abcde' -> '780f771abcde'; '' if invalid."""
    hexed = re.sub(r"[^0-9A-Fa-f]", "", mac or "").lower()
    return hexed if len(hexed) == 12 else ""


def feature_label(key: str) -> str:
    return key.replace("_", " ")


def resolve_feature(device: Device, words: list[str]) -> str | None:
    """Map user words to a feature key of this device, or None.

    `on`/`off` -> power_on/power_off; `on high` -> power_on_high; `temp up` -> temp_up.
    All words must be used: trailing junk never gets silently dropped.
    Only keys in device.features can ever be returned.
    """
    words = [w.lower() for w in words if w]
    if not words:
        return None
    joined = "_".join(words)
    candidates = [joined]
    if words[0] in ("on", "off"):
        candidates.insert(0, "power_" + joined)
    if joined in ALIASES:
        candidates.append(ALIASES[joined])
    for c in candidates:
        if c in device.features:
            return c
    return None


def _dig(tree: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(tree, dict):
            return None
        tree = tree.get(k)
    return tree


def _decode(hex_value: Any) -> bytes:
    if isinstance(hex_value, list):
        if not all(isinstance(x, str) for x in hex_value):
            raise ValueError("hex list must contain strings")
        hex_value = "".join(hex_value)
    if not isinstance(hex_value, str):
        raise ValueError("not a hex string")
    data = bytes.fromhex(hex_value)
    if not data:
        raise ValueError("empty")
    return data


def build_ir_features(device_type: DeviceType, brand: str, codes: dict[str, Any],
                      warn: list[str], where: str) -> dict[str, Feature]:
    idempotent = device_type is DeviceType.AIRCON
    decoded: dict[str, bytes] = {}
    for key, value in codes.items():
        if key == "digits":
            continue  # set-top box channel digits: not supported in v2 (B3 not fixed)
        if not (isinstance(key, str) and FEATURE_RE.fullmatch(key)):
            warn.append(f"{where}: skipped code '{key}' (bad name)")
            continue
        try:
            data = _decode(value)
        except ValueError as e:
            warn.append(f"{where}: skipped code '{key}' ({e})")
            continue
        if data[0] not in PACKET_TYPES:
            warn.append(f"{where}: code '{key}' has unknown packet type 0x{data[0]:02x}")
        decoded[key] = data

    features = {k: Feature(k, feature_label(k), (v,), idempotent=idempotent) for k, v in decoded.items()}

    # Epson EB projectors power off with "standby" pressed twice, 2s apart
    if brand.lower() == "epsoneb" and "power_off" not in features and "standby" in decoded:
        features["power_off"] = Feature("power_off", "power off", (decoded["standby"],) * 2, gap_s=2.0)

    ordered = {k: features[k] for k in FEATURE_ORDER if k in features}
    ordered.update((k, f) for k, f in features.items() if k not in ordered)
    return ordered


def build_plug_features(bl_type: str) -> dict[str, Feature]:
    ops = ["power_on", "power_off", "check_power"]
    if bl_type == "SP4":
        ops += ["nightlight_on", "nightlight_off", "check_nightlight"]
    return {op: Feature(op, feature_label(op), plug_op=op) for op in ops}


def load_registry(devices_cfg: dict[str, Any], commands: dict[str, Any]) -> Registry:
    """Build rooms and devices. Problems become warnings; one bad entry never drops
    a whole room (B10), and a duplicate id never overwrites the first one (B19)."""
    reg = Registry()
    warn = reg.warnings
    if not isinstance(devices_cfg, dict):
        warn.append("devices.json: top level must be an object of rooms")
        return reg
    if not isinstance(commands, dict):
        warn.append("commands.json: top level must be an object; no IR codes loaded")
        commands = {}

    for room_name, rcfg in devices_cfg.items():
        if not isinstance(rcfg, dict):
            warn.append(f"room '{room_name}': not an object, skipped")
            continue
        if not ID_RE.fullmatch(room_name):
            warn.append(f"room '{room_name}': name must match {ID_RE.pattern}, skipped")
            continue
        room = Room(room_name)
        if rcfg.get("mac_address"):
            room.rm_mac = normalize_mac(str(rcfg["mac_address"]))
            room.rm_type = str(rcfg.get("broadlink_type", "")).upper()
            room.rm_ip = str(rcfg.get("ip_address", "") or "")
            if not room.rm_mac:
                warn.append(f"room '{room_name}': bad RM mac_address '{rcfg['mac_address']}'")
        reg.rooms[room_name] = room

        for key, add in (("devices", _add_ir_device), ("broadlink_devices", _add_plug)):
            entries = rcfg.get(key) or []
            if not isinstance(entries, list):
                warn.append(f"room '{room_name}': '{key}' must be a list, skipped")
                continue
            for d in entries:
                add(reg, room, d, commands)

        if any(reg.devices[i].kind == "ir" for i in room.devices) and not room.rm_mac:
            warn.append(f"room '{room_name}': has IR devices but no RM mac_address; they cannot send")

    return reg


def _claim_id(reg: Registry, room: Room, entry: Any) -> str | None:
    if not isinstance(entry, dict):
        reg.warnings.append(f"room '{room.name}': device entry is not an object, skipped")
        return None
    dev_id = entry.get("id")
    if not (isinstance(dev_id, str) and ID_RE.fullmatch(dev_id)):
        reg.warnings.append(f"room '{room.name}': device id '{dev_id}' must match {ID_RE.pattern}, skipped")
        return None
    if dev_id in reg.devices or dev_id in reg.rooms:
        reg.warnings.append(f"room '{room.name}': duplicate id '{dev_id}' skipped (first one kept)")
        return None
    return dev_id


def _add_ir_device(reg: Registry, room: Room, entry: Any, commands: dict[str, Any]) -> None:
    dev_id = _claim_id(reg, room, entry)
    if dev_id is None:
        return
    raw_type = entry.get("type")
    try:
        if isinstance(raw_type, (bool, float)):
            raise TypeError
        dtype = DeviceType(int(raw_type))
    except (TypeError, ValueError):
        reg.warnings.append(f"device '{dev_id}': unknown type {entry.get('type')!r}, skipped")
        return
    brand = str(entry.get("brand", ""))
    model = str(entry.get("model", ""))
    codes = _dig(commands, str(int(dtype)), brand, model)
    if not isinstance(codes, dict):
        reg.warnings.append(f"device '{dev_id}': no IR codes for {int(dtype)}/{brand}/{model} in commands.json")
        codes = {}
    features = build_ir_features(dtype, brand, codes, reg.warnings, f"device '{dev_id}'")
    reg.devices[dev_id] = Device(
        id=dev_id, room=room.name, kind="ir", type_name=dtype.name.lower(),
        features=features, brand=brand, model=model, device_type=dtype,
    )
    room.devices.append(dev_id)


def _add_plug(reg: Registry, room: Room, entry: Any, _commands: Any = None) -> None:
    dev_id = _claim_id(reg, room, entry)
    if dev_id is None:
        return
    bl_type = str(entry.get("broadlink_type", "")).upper()
    if bl_type not in PLUG_TYPES:
        reg.warnings.append(f"device '{dev_id}': unsupported broadlink_type '{bl_type}', skipped")
        return
    mac = normalize_mac(str(entry.get("mac_address", "")))
    if not mac:
        reg.warnings.append(f"device '{dev_id}': bad mac_address, skipped")
        return
    reg.devices[dev_id] = Device(
        id=dev_id, room=room.name, kind="plug", type_name=bl_type.lower(),
        features=build_plug_features(bl_type), mac=mac, bl_type=bl_type,
    )
    room.devices.append(dev_id)
