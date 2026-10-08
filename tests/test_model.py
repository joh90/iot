import json

import pytest
from pathlib import Path

from iotbot.devices.model import (
    DeviceType, build_ir_features, load_registry, normalize_mac, resolve_feature,
)

ROOT = Path(__file__).resolve().parent.parent
REAL_COMMANDS = json.loads((ROOT / "commands.json").read_text())

ON = "2600" + "0a" * 8
OFF = "2600" + "0b" * 8


def cmds(**codes):
    return {"1": {"daikin": {"nx": codes}}, "2": {"lg": {"tv1": {"power_on": ON, "mute": OFF}}}}


def test_normalize_mac():
    assert normalize_mac("78:0F:77:1A:BC:DE") == "780f771abcde"
    assert normalize_mac("780f771abcde") == "780f771abcde"
    assert normalize_mac("78:0f") == ""


def test_registry_basic_and_feature_order():
    cfg = {"bedroom": {"mac_address": "78:0f:77:1a:bc:de", "broadlink_type": "rmmini",
                       "devices": [{"type": 1, "id": "bedroom_ac", "brand": "daikin", "model": "nx"}]}}
    reg = load_registry(cfg, cmds(toggle_swing=ON, power_off=OFF, power_on=ON))
    assert reg.warnings == []
    room = reg.rooms["bedroom"]
    assert room.rm_mac == "780f771abcde" and room.rm_type == "RMMINI"
    ac = reg.devices["bedroom_ac"]
    assert list(ac.features) == ["power_on", "power_off", "toggle_swing"]
    assert ac.features["power_on"].codes == (bytes.fromhex(ON),)
    assert ac.features["power_on"].idempotent is True
    assert ac.device_type is DeviceType.AIRCON


def test_non_aircon_not_idempotent():
    cfg = {"lr": {"mac_address": "780f771abcde", "broadlink_type": "RMMINI",
                  "devices": [{"type": 2, "id": "tv", "brand": "lg", "model": "tv1"}]}}
    reg = load_registry(cfg, cmds())
    assert reg.devices["tv"].features["power_on"].idempotent is False


def test_duplicate_ids_rejected_first_kept():
    cfg = {
        "a": {"mac_address": "780f771abcde", "broadlink_type": "RMMINI",
              "devices": [{"type": 1, "id": "ac", "brand": "daikin", "model": "nx"}]},
        "b": {"mac_address": "780f771abcdf", "broadlink_type": "RMMINI",
              "devices": [{"type": 2, "id": "ac", "brand": "lg", "model": "tv1"}]},
    }
    reg = load_registry(cfg, cmds(power_on=ON))
    assert reg.devices["ac"].room == "a"
    assert reg.rooms["b"].devices == []
    assert any("duplicate id 'ac'" in w for w in reg.warnings)


def test_device_id_cannot_shadow_room():
    cfg = {"a": {"mac_address": "780f771abcde", "broadlink_type": "RMMINI",
                 "devices": [{"type": 1, "id": "a", "brand": "daikin", "model": "nx"}]}}
    reg = load_registry(cfg, cmds(power_on=ON))
    assert "a" not in reg.devices


def test_bad_entries_do_not_drop_room():
    cfg = {"r": {"mac_address": "780f771abcde", "broadlink_type": "RMMINI", "devices": [
        {"type": 99, "id": "x", "brand": "b", "model": "m"},
        {"type": 1, "id": "bad id!", "brand": "daikin", "model": "nx"},
        "nonsense",
        {"type": 1, "id": "ok", "brand": "daikin", "model": "nx"},
        {"type": 1, "id": "nocodes", "brand": "daikin", "model": "zzz"},
    ]}}
    reg = load_registry(cfg, cmds(power_on=ON, broken="zz"))
    assert set(reg.devices) == {"ok", "nocodes"}
    assert reg.devices["nocodes"].features == {}
    assert "broken" not in reg.devices["ok"].features
    assert len(reg.warnings) == 5


def test_ir_without_rm_warns():
    cfg = {"r": {"devices": [{"type": 1, "id": "ac", "brand": "daikin", "model": "nx"}]}}
    reg = load_registry(cfg, cmds(power_on=ON))
    assert any("no RM" in w for w in reg.warnings)


def test_plugs():
    cfg = {"r": {"broadlink_devices": [
        {"id": "lamp", "mac_address": "78:0f:77:11:6d:ef", "broadlink_type": "sp4"},
        {"id": "lamp2", "mac_address": "780f77116df0", "broadlink_type": "SP2"},
        {"id": "weird", "mac_address": "780f77116df1", "broadlink_type": "MP1"},
    ]}}
    reg = load_registry(cfg, {})
    assert reg.devices["lamp"].kind == "plug" and "nightlight_on" in reg.devices["lamp"].features
    assert "nightlight_on" not in reg.devices["lamp2"].features
    assert "weird" not in reg.devices


def test_resolve_feature_allowlist():
    cfg = {"r": {"mac_address": "780f771abcde", "broadlink_type": "RMMINI",
                 "devices": [{"type": 1, "id": "ac", "brand": "daikin", "model": "nx"}]}}
    reg = load_registry(cfg, cmds(power_on=ON, power_off=OFF, power_on_high=ON, temp_up=ON))
    ac = reg.devices["ac"]
    assert resolve_feature(ac, ["on"]) == "power_on"
    assert resolve_feature(ac, ["off"]) == "power_off"
    assert resolve_feature(ac, ["power", "on", "high"]) == "power_on_high"
    assert resolve_feature(ac, ["TEMP", "up"]) == "temp_up"
    assert resolve_feature(ac, ["on", "high"]) == "power_on_high"
    assert resolve_feature(ac, ["off", "now"]) is None
    assert resolve_feature(ac, ["temp", "up", "please"]) is None
    for evil in (["__delattr__"], ["fire_action"], ["room"], ["features"], []):
        assert resolve_feature(ac, evil) is None


def test_epson_standby_macro():
    feats = build_ir_features(DeviceType.PROJECTOR, "epsoneb", {"standby": ON}, [], "p")
    assert feats["power_off"].codes == (bytes.fromhex(ON),) * 2
    assert feats["power_off"].gap_s == 2.0


def test_real_commands_json_decodes():
    """Every capture in the repo decodes to a Broadlink IR packet."""
    warn = []
    for t, brands in REAL_COMMANDS.items():
        for brand, models in brands.items():
            for model, codes in models.items():
                if not isinstance(codes, dict):
                    continue  # samsung set-top box has no model level (B3, not fixed)
                feats = build_ir_features(DeviceType(int(t)), brand, codes, warn, f"{t}/{brand}/{model}")
                assert feats, (t, brand, model)
                for f in feats.values():
                    assert all(c[0] == 0x26 for c in f.codes)
    assert warn == []


def test_aliases_for_old_words():
    cmds_ = {"2": {"p": {"tv": {"mute": ON}}}, "5": {"c": {"amp": {"input": ON}}}}
    cfg = {"r": {"mac_address": "780f771abcde", "devices": [
        {"type": 2, "id": "tv", "brand": "p", "model": "tv"},
        {"type": 5, "id": "amp", "brand": "c", "model": "amp"}]}}
    reg = load_registry(cfg, cmds_)
    assert resolve_feature(reg.devices["tv"], ["unmute"]) == "mute"
    assert resolve_feature(reg.devices["amp"], ["change", "input"]) == "input"
    assert "unmute" not in reg.devices["tv"].features  # alias only, no duplicate button


@pytest.mark.parametrize("commands", [None, [], {"1": None}, {"1": {"daikin": []}}, {"1": {"daikin": {"nx": 5}}}])
def test_malformed_commands_never_raise(commands):
    cfg = {"r": {"mac_address": "780f771abcde",
                 "devices": [{"type": 1, "id": "ac", "brand": "daikin", "model": "nx"}]}}
    reg = load_registry(cfg, commands)
    assert reg.devices["ac"].features == {}
    assert reg.warnings


@pytest.mark.parametrize("cfg", [
    {"r": {"mac_address": 123, "devices": 5}},
    {"r": {"devices": "abc"}},
    {"r": {"devices": [{"type": True, "id": "x", "brand": "b", "model": "m"}]}},
    {"r": {"devices": [{"type": 1, "id": None, "brand": "b", "model": "m"}]}},
    {"r": {"devices": [{"type": 1, "id": "a\n", "brand": "b", "model": "m"}]}},
    {"r": {"devices": [{"type": 1, "id": "ok", "brand": "daikin", "model": "nx"}]}},
])
def test_malformed_devices_never_raise(cfg):
    reg = load_registry(cfg, cmds(power_on=["2600", 5]))
    assert set(reg.devices) <= {"ok"}
    assert len([w for w in reg.warnings if "characters" in w]) == 0
