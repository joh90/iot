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
    assert svc.resolve("bedroom_ac", ["on"]).data == {"device": "bedroom_ac", "feature": "power_on"}
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


@pytest.mark.parametrize("raw", ["abc", "-5", "0", "", None, "+5", "1_0", "--5", "\u00b2", True])
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


@pytest.mark.parametrize("key", ["-5", "--5", "\u00b2", "0", "007"])
def test_bad_user_keys_refused(tmp_path, key):
    p = tmp_path / "users.json"
    p.write_text(json.dumps({key: "x"}))
    with pytest.raises(StoreError):
        JsonStore(p, dict, validate=validate_users).load()


async def test_delete_nonexistent(tmp_path):
    assert (await users(tmp_path).delete(ALICE, 999)).error == "not_found"


async def test_audit_lines(tmp_path):
    u = users(tmp_path)
    await u.add(ALICE, 42, "Bob")
    await u.delete(ALICE, 42)
    lines = [json.loads(x) for f in tmp_path.glob("audit-*.jsonl") for x in f.read_text().splitlines()]
    assert [x["event"] for x in lines] == ["user_added", "user_removed"]


async def test_store_failure_returns_result(tmp_path, monkeypatch):
    import iotbot.store as store_mod

    u = users(tmp_path)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(store_mod, "write_json_atomic", boom)
    r = await u.add(ALICE, 42, "Bob")
    assert r.error == "store_error" and not u.is_allowed(42)
    assert not list(tmp_path.glob("audit-*.jsonl"))


async def test_name_cleaned_and_truncated(tmp_path):
    u = users(tmp_path)
    await u.add(ALICE, 7, "A\x00b\nc " + "x" * 50)
    assert u.name_of(7).startswith("A b c ") and len(u.name_of(7)) <= 32


async def test_only_people_change_users(tmp_path):
    u = users(tmp_path)
    for surface in ("llm", "scheduler", "system"):
        assert (await u.add(Actor(1, "A", surface), 5, "X")).error == "forbidden"
        assert (await u.delete(Actor(1, "A", surface), 5)).error == "forbidden"


async def test_concurrent_add_same_id_one_wins(tmp_path):
    import asyncio

    u = users(tmp_path)
    rs = await asyncio.gather(*(u.add(ALICE, 9, f"N{i}") for i in range(5)))
    assert sum(r.ok for r in rs) == 1


async def test_plug_failure(tmp_path):
    r = await service(tmp_path, FakeHub(fail=HubError("offline"))).run("lamp", "power_on", ALICE)
    assert not r.ok


async def test_seq_and_prev_feature(tmp_path):
    svc = service(tmp_path)
    r1 = await svc.run("bedroom_ac", "power_on", ALICE, request_id="u1")
    r2 = await svc.run("bedroom_ac", "power_off", ALICE, source="sched:s1")
    assert r2.data["seq"] > r1.data["seq"]
    assert svc.last["bedroom_ac"].prev_feature == "power_on"
    ev = events(tmp_path)
    assert ev[0]["request_id"] == "u1" and ev[1]["source"] == "sched:s1"
    assert r1.to_dict()["ok"] is True


async def test_cancelled_send_is_logged(tmp_path):
    import asyncio

    class Hang(FakeHub):
        async def send_ir(self, *a, **k):
            await asyncio.sleep(10)

    svc = service(tmp_path, Hang())
    task = asyncio.create_task(svc.run("bedroom_ac", "power_on", ALICE))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    ev = events(tmp_path)
    assert ev and ev[0]["error"] == "cancelled" and ev[0]["maybe_delivered"] is True
