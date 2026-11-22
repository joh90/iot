import json

import pytest

from iotbot.ac.state import FANS, VANES, AcState, AcStateError, AcStateStore

BED = AcState(power=True, temp=22, fan=3, vane="auto")


def test_defaults_and_describe():
    s = AcState(power=True, temp=24)
    assert (s.fan, s.vane, s.mode, s.powerful) == ("auto", "auto", "cool", False)
    assert BED.describe() == "ON cool 22C fan 3 vane auto"
    assert BED.with_changes(power=False).describe() == "OFF (cool 22C fan 3 vane auto)"
    assert BED.with_changes(powerful=True).describe() == "ON cool 22C fan 3 vane auto powerful"


@pytest.mark.parametrize("changes", [
    {"temp": 15}, {"temp": 32}, {"temp": 22.5}, {"temp": "22"}, {"temp": True},
    {"fan": 5}, {"fan": 0}, {"fan": True}, {"fan": "3"}, {"fan": "max"},
    {"vane": 6}, {"vane": 0}, {"vane": False}, {"vane": "middle"},
    {"mode": "dry"}, {"mode": "fan"}, {"mode": "heat"},
    {"power": 1}, {"power": "on"}, {"powerful": 1},
    {"fan": 3.0}, {"vane": 2.0}, {"temp": 22.0},
])
def test_invalid_values_rejected(changes):
    with pytest.raises(AcStateError):
        BED.with_changes(**changes)


def test_unknown_key_rejected():
    with pytest.raises(AcStateError, match="unknown AC setting"):
        BED.with_changes(swing_h=3)


def test_all_valid_values_accepted():
    for t in range(16, 32):
        for f in FANS:
            for v in VANES:
                AcState(power=False, temp=t, fan=f, vane=v)


def test_dict_round_trip_and_strict_parse():
    assert AcState.from_dict(BED.to_dict()) == BED
    assert AcState.from_dict(json.loads(json.dumps(BED.to_dict()))) == BED
    with pytest.raises(AcStateError):
        AcState.from_dict({**BED.to_dict(), "extra": 1})
    with pytest.raises(AcStateError):
        AcState.from_dict({"power": True})
    with pytest.raises(AcStateError):
        AcState.from_dict(["not", "a", "dict"])


def test_with_changes_leaves_original():
    hot = BED.with_changes(temp=28)
    assert (BED.temp, hot.temp) == (22, 28)


def test_frozen():
    with pytest.raises(AttributeError):
        BED.temp = 30


async def test_store_put_get_and_reload(tmp_path):
    p = tmp_path / "state" / "ac_state.json"
    store = AcStateStore(p)
    store.load()
    assert store.load_warning is None
    assert store.get("bedroom-aircon") is None
    await store.put("bedroom-aircon", BED, "Alice", at=100.0)
    again = AcStateStore(p)
    again.load()
    got = again.get("bedroom-aircon")
    assert (got.state, got.at, got.actor) == (BED, 100.0, "Alice")


def test_store_corrupt_file_starts_empty(tmp_path):
    p = tmp_path / "ac_state.json"
    p.write_text("{not json")
    store = AcStateStore(p)
    store.load()
    assert "starting with no remembered AC state" in store.load_warning
    assert store.get("x") is None


async def test_store_corrupt_file_then_save_works(tmp_path):
    p = tmp_path / "ac_state.json"
    p.write_text("[]")
    store = AcStateStore(p)
    store.load()
    assert store.load_warning
    await store.put("x", BED, "Bob", at=1.0)
    assert json.loads(p.read_text())["x"]["actor"] == "Bob"


def test_store_bad_entry_skipped(tmp_path):
    p = tmp_path / "ac_state.json"
    p.write_text(json.dumps({
        "good": {"state": BED.to_dict(), "at": 5, "actor": "A"},
        "bad_state": {"state": {**BED.to_dict(), "temp": 99}, "at": 5, "actor": "A"},
        "no_at": {"state": BED.to_dict(), "actor": "A"},
        "not_obj": 3,
        "bool_at": {"state": BED.to_dict(), "at": True, "actor": "A"},
        "str_at": {"state": BED.to_dict(), "at": "1e3", "actor": "A"},
    }))
    store = AcStateStore(p)
    store.load()
    assert store.get("good").state == BED
    for k in ("bad_state", "no_at", "not_obj", "bool_at", "str_at"):
        assert store.get(k) is None


async def test_store_corrupt_main_loads_good_bak(tmp_path):
    p = tmp_path / "ac_state.json"
    store = AcStateStore(p)
    store.load()
    await store.put("x", BED, "A", at=1.0)
    await store.put("x", BED.with_changes(temp=25), "A", at=2.0)
    p.write_text("{torn")
    again = AcStateStore(p)
    again.load()
    assert "loaded ac_state.json.bak" in again.load_warning
    assert again.get("x").state == BED


async def test_store_rejects_nan_timestamp(tmp_path):
    store = AcStateStore(tmp_path / "ac_state.json")
    store.load()
    with pytest.raises(ValueError):
        await store.put("x", BED, "A", at=float("nan"))
    assert store.get("x") is None
