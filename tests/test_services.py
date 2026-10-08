import json

import pytest

from iotbot.devices.hub import BroadlinkHub, Expected, HubError
from iotbot.devices.model import load_registry
from iotbot.result import Actor
from iotbot.services.devices import DeviceService
from iotbot.services.users import UserService, validate_users
from iotbot.store import JsonlLog, JsonStore, StoreError

ON = "2600" + "0a" * 8
ALICE = Actor(1, "Alice", "slash")


class FakeHub:
    def __init__(self, fail=None):
        self.sent = []
        self.fail = fail
        self.plug_state = {}

    async def send_ir(self, mac, packets, gap_s=0.0, idempotent=False):
        if self.fail:
            raise self.fail
        self.sent.append((mac, packets, gap_s, idempotent))

    async def plug(self, mac, op):
        if self.fail:
            raise self.fail
        if op.startswith("power_"):
            self.plug_state[mac] = op == "power_on"
        return self.plug_state.get(mac, False)


def registry():
    cfg = {
        "bedroom": {"mac_address": "780f771abcde", "broadlink_type": "RMMINI",
                    "devices": [{"type": 1, "id": "bedroom_ac", "brand": "daikin", "model": "nx"}],
                    "broadlink_devices": [{"id": "lamp", "mac_address": "780f77116def", "broadlink_type": "SP2"}]},
        "norm": {"devices": [{"type": 1, "id": "orphan_ac", "brand": "daikin", "model": "nx"}]},
    }
    return load_registry(cfg, {"1": {"daikin": {"nx": {"power_on": ON, "power_off": ON}}}})


def service(tmp_path, hub=None):
    return DeviceService(registry(), hub or FakeHub(), JsonlLog(tmp_path, "device_events"))


def events(tmp_path):
    lines = []
    for f in sorted(tmp_path.glob("device_events-*.jsonl")):
        lines += [json.loads(x) for x in f.read_text().splitlines()]
    return lines


async def test_run_ir_sends_and_logs(tmp_path):
    hub = FakeHub()
    svc = service(tmp_path, hub)
    r = await svc.run("bedroom_ac", "power_on", ALICE)
    assert r.ok and r.message == "Sent bedroom_ac power on"
    assert hub.sent[0][0] == "780f771abcde" and hub.sent[0][3] is True
    ev = events(tmp_path)[0]
    assert ev["device"] == "bedroom_ac" and ev["ok"] and ev["surface"] == "slash" and ev["user_id"] == 1
    assert svc.last["bedroom_ac"].feature == "power_on"


async def test_run_rejects_non_allowlisted(tmp_path):
    hub = FakeHub()
    svc = service(tmp_path, hub)
    for feat in ("__delattr__", "fire_action", "toggle_swing"):
        r = await svc.run("bedroom_ac", feat, ALICE)
        assert not r.ok and r.error == "feature_not_found"
    assert hub.sent == [] and events(tmp_path) == []


async def test_run_failure_reported_and_logged(tmp_path):
    svc = service(tmp_path, FakeHub(fail=HubError("no reply", maybe_delivered=True)))
    r = await svc.run("bedroom_ac", "power_on", ALICE)
    assert not r.ok and r.error == "send_failed" and r.data["maybe_delivered"]
    ev = events(tmp_path)[0]
    assert ev["ok"] is False and ev["maybe_delivered"] is True


async def test_unexpected_exception_does_not_escape(tmp_path):
    svc = service(tmp_path, FakeHub(fail=RuntimeError("boom")))
    r = await svc.run("bedroom_ac", "power_on", ALICE)
    assert not r.ok


async def test_room_without_rm(tmp_path):
    r = await service(tmp_path).run("orphan_ac", "power_on", ALICE)
    assert not r.ok and "no RM" in r.message


async def test_plug(tmp_path):
    svc = service(tmp_path)
    r = await svc.run("lamp", "power_on", ALICE)
    assert r.ok and r.message == "lamp power is ON"


async def test_resolve(tmp_path):
    svc = service(tmp_path)
    assert svc.resolve("bedroom_ac", ["on"]).data[1] == "power_on"
    assert svc.resolve("nope", ["on"]).error == "device_not_found"
    assert svc.resolve("bedroom_ac", []).error == "feature_missing"
    r = svc.resolve("bedroom_ac", ["dance"])
    assert r.error == "feature_not_found" and "power on" in r.message


def users(tmp_path, initial=None):
    p = tmp_path / "users.json"
    p.write_text(json.dumps(initial if initial is not None else {"1": "Alice"}))
    st = JsonStore(p, dict, validate=validate_users)
    st.load()
    return UserService(st, JsonlLog(tmp_path, "audit"))


async def test_auth_by_id_only(tmp_path):
    u = users(tmp_path)
    assert u.is_allowed(1)
    assert not u.is_allowed(2)
    assert not u.is_allowed(None)


async def test_add_and_delete(tmp_path):
    u = users(tmp_path)
    r = await u.add(ALICE, "42", "  Bob   Tan ")
    assert r.ok and u.name_of(42) == "Bob Tan"
    assert json.loads((tmp_path / "users.json").read_text())["42"] == "Bob Tan"
    assert not (await u.add(ALICE, 42, "Again")).ok
    r = await u.delete(ALICE, 42)
    assert r.ok and not u.is_allowed(42)
    # double tap (B13)
    r = await u.delete(ALICE, 42)
    assert not r.ok and r.error == "not_found"


async def test_cannot_delete_self(tmp_path):
    u = users(tmp_path)
    r = await u.delete(ALICE, "1")
    assert r.error == "self_delete" and u.is_allowed(1)


@pytest.mark.parametrize("raw", ["abc", "-5", "0", "", None])
async def test_bad_ids(tmp_path, raw):
    u = users(tmp_path)
    assert (await u.add(ALICE, raw, "X")).error == "bad_user_id"


async def test_empty_name_rejected(tmp_path):
    assert (await users(tmp_path).add(ALICE, 5, "   ")).error == "bad_name"


async def test_same_names_allowed_sorted(tmp_path):
    u = users(tmp_path, {"3": "Sam", "2": "Sam", "1": "alice"})
    assert u.list() == [(1, "alice"), (2, "Sam"), (3, "Sam")]


def test_invalid_users_file_refused(tmp_path):
    p = tmp_path / "users.json"
    p.write_text('{"alice": "x"}')
    st = JsonStore(p, dict, validate=validate_users)
    with pytest.raises(StoreError):
        st.load()
