import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from iotbot.ac.state import AcState
from iotbot.schedule import model as sm
from iotbot.schedule.store import ScheduleStore
from iotbot.store import write_json_atomic

SGT = ZoneInfo("Asia/Singapore")
T0 = datetime(2026, 10, 10, 12, 0, tzinfo=SGT)
BED = AcState(power=True, temp=22, fan=3, vane="auto")


def weekly(sid="s7f3a", **kw):
    base = dict(id=sid, device="bed_ac", action=sm.On(BED), when=sm.make_weekly("23:00", ["sun", "mon"]),
                created_by=1, created_at=T0, updated_by=1, updated_at=T0)
    return sm.Schedule(**{**base, **kw})


def timer(sid="t91c2", **kw):
    return weekly(sid, **{"action": sm.Off(), "when": sm.Once(datetime(2026, 10, 11, 1, 30, tzinfo=SGT)), **kw})


def raw(s):
    return json.loads(json.dumps(s.to_dict()))


# ---- round trip ------------------------------------------------------------------------

@pytest.mark.parametrize("s", [
    weekly(),
    timer(),
    weekly(label="Bedtime", rev=4, enabled=False, paused_until=T0, skip_dates=(date(2026, 10, 11), date(2026, 10, 12)),
           last_fired=sm.LastFired(T0, "s7f3a@2026-10-09T23:00:00+08:00", "ok")),
    weekly(action=sm.make_adjust({"temp": 24, "fan": "auto"})),
    weekly(action=sm.Capture("power_on"), device="tv"),
])
def test_round_trip(s):
    assert sm.parse_schedule(raw(s)) == s


def test_weekly_days_kept_in_week_order():
    assert sm.make_weekly("07:30", ["sun", "mon", "fri"]).days == ("mon", "fri", "sun")
    r = raw(weekly())
    r["when"]["days"] = ["sun", "mon"]
    with pytest.raises(sm.ScheduleError, match="order"):
        sm.parse_schedule(r)


def test_adjust_changes_sorted_and_applied():
    a = sm.make_adjust({"temp": 25, "fan": 1})
    assert a.changes == (("fan", 1), ("temp", 25))
    assert a.apply(BED) == BED.with_changes(temp=25, fan=1)


# ---- rejects ---------------------------------------------------------------------------

def _set(path, value):
    def f(r):
        *keys, last = path
        for k in keys:
            r = r[k]
        r[last] = value
    return f


@pytest.mark.parametrize("change,match", [
    (_set(["id"], "x1234"), "bad id"),
    (_set(["id"], "t7f3a"), "does not match"),
    (_set(["rev"], 0), "rev"),
    (_set(["rev"], True), "rev"),
    (_set(["label"], "  two  spaces"), "label"),
    (_set(["label"], "x" * 33), "label"),
    (_set(["device"], "bad:id"), "device"),
    (_set(["enabled"], 1), "enabled"),
    (_set(["only_if"], {"home": True}), "only_if"),
    (_set(["skip_dates"], ["2026-10-12", "2026-10-11"]), "sorted"),
    (_set(["skip_dates"], ["2026-10-11", "2026-10-11"]), "sorted"),
    (_set(["skip_dates"], ["tomorrow"]), "skip_dates"),
    (_set(["paused_until"], "2026-10-11T00:00:00"), "timezone"),
    (_set(["created_by"], -1), "created_by"),
    (_set(["created_by"], "1"), "created_by"),
    (_set(["last_fired"], {"at": "2026-10-10T12:00:00+08:00", "occurrence": "x", "result": "meh"}), "last_fired"),
    (_set(["when", "time"], "24:00"), "HH:MM"),
    (_set(["when", "time"], "7:30"), "HH:MM"),
    (_set(["when", "days"], []), "at least one"),
    (_set(["when", "days"], ["mon", "mon"]), "twice"),
    (_set(["when", "days"], ["funday"]), "unknown day"),
    (_set(["when"], {"kind": "daily", "time": "23:00"}), "unknown when"),
    (_set(["action"], {"kind": "on", "state": {**BED.to_dict(), "power": False}}), "power true"),
    (_set(["action"], {"kind": "on", "state": {**BED.to_dict(), "temp": 40}}), "temp"),
    (_set(["action"], {"kind": "adjust", "changes": {"power": False}}), "adjust can change"),
    (_set(["action"], {"kind": "adjust", "changes": {}}), "at least one"),
    (_set(["action"], {"kind": "adjust", "changes": {"temp": 99}}), "temp"),
    (_set(["action"], {"kind": "capture", "key": "Power On"}), "feature key"),
    (_set(["action"], {"kind": "off", "extra": 1}), "unknown action"),
    (_set(["action"], {"kind": "toggle"}), "unknown action"),
])
def test_rejects(change, match):
    r = raw(weekly())
    change(r)
    with pytest.raises(sm.ScheduleError, match=match):
        sm.parse_schedule(r)


def test_rejects_missing_and_extra_fields():
    r = raw(weekly())
    del r["rev"]
    r["colour"] = "red"
    with pytest.raises(sm.ScheduleError, match="missing \\['rev'\\] / unexpected \\['colour'\\]"):
        sm.parse_schedule(r)


def test_once_rejects_microseconds():
    r = raw(timer())
    r["when"]["at"] = "2026-10-11T01:30:00.123456+08:00"
    with pytest.raises(sm.ScheduleError, match="whole seconds"):
        sm.parse_schedule(r)


@pytest.mark.parametrize("field,value", [("skip_dates", ["2026-10-11"]),
                                         ("paused_until", "2026-10-11T00:00:00+08:00")])
def test_timer_rejects_skip_and_pause(field, value):
    r = raw(timer())
    r[field] = value
    with pytest.raises(sm.ScheduleError, match="timer cannot"):
        sm.parse_schedule(r)


@pytest.mark.parametrize("data", [{"version": True, "schedules": {}}, {"version": 1, "schedules": {}, "x": 1},
                                  {"version": 1, "schedules": {}, "saved_at": "now"},
                                  {"version": 1, "schedules": {}, "saved_at": float("nan")}])
def test_validate_file_strict(data):
    with pytest.raises((TypeError, ValueError)):
        sm.validate_file(data)
    sm.validate_file({"version": 1, "schedules": {}, "saved_at": 1.5})


def test_new_id_prefix_and_free():
    taken = {"s0000"}
    for _ in range(50):
        sid = sm.new_id(False, taken)
        assert sm.ID_RE.fullmatch(sid) and sid[0] == "s" and sid not in taken
        taken.add(sid)
    assert sm.new_id(True, taken)[0] == "t"


def test_clean_label():
    assert sm.clean_label("  Bed\ttime\n ") == "Bed time"
    assert len(sm.clean_label("x" * 50)) == sm.MAX_LABEL


# ---- store -----------------------------------------------------------------------------

async def test_store_saves_and_reloads(tmp_path):
    p = tmp_path / "schedules.json"
    st = ScheduleStore(p)
    st.load()
    assert st.all() == {} and st.load_warning is None
    await st.mutate(lambda d: d.update({"s7f3a": weekly(), "t91c2": timer()}))
    st2 = ScheduleStore(p)
    st2.load()
    assert st2.all() == {"s7f3a": weekly(), "t91c2": timer()}
    assert json.loads(p.read_text())["version"] == 1
    await st2.mutate(lambda d: d.pop("t91c2"))
    assert set(st2.all()) == {"s7f3a"} and set(json.loads(p.read_text())["schedules"]) == {"s7f3a"}


async def test_store_keeps_broken_entries(tmp_path):
    p = tmp_path / "schedules.json"
    bad = raw(weekly("s0bad"))
    bad["when"]["time"] = "25:00"
    write_json_atomic(p, {"version": 1, "schedules": {"s7f3a": raw(weekly()), "s0bad": bad,
                                                      "s0key": raw(weekly("s1234"))}})
    st = ScheduleStore(p)
    st.load()
    assert set(st.all()) == {"s7f3a"}
    assert "HH:MM" in st.broken["s0bad"] and "stored under" in st.broken["s0key"]
    assert st.ids() == {"s7f3a", "s0bad", "s0key"}
    await st.mutate(lambda d: d.pop("s7f3a"))
    saved = json.loads(p.read_text())["schedules"]
    assert saved == {"s0bad": bad, "s0key": raw(weekly("s1234"))}     # broken ones untouched
    assert await st.drop_broken("s0bad") and not await st.drop_broken("s7f3a")
    assert set(json.loads(p.read_text())["schedules"]) == {"s0key"}


async def test_store_mutate_failure_changes_nothing(tmp_path):
    st = ScheduleStore(tmp_path / "schedules.json")
    st.load()

    def boom(d):
        d["s7f3a"] = weekly()
        raise RuntimeError("no")

    with pytest.raises(RuntimeError):
        await st.mutate(boom)
    assert st.all() == {} and not (tmp_path / "schedules.json").exists()
    with pytest.raises(sm.ScheduleError, match="put under"):
        await st.mutate(lambda d: d.update({"s0000": weekly()}))
    assert st.all() == {}


def test_store_unusable_file_moved_aside(tmp_path):
    p = tmp_path / "schedules.json"
    p.write_text('{"version": 2, "schedules": {}}')
    st = ScheduleStore(p)
    st.load()
    assert st.all() == {} and "starting with no schedules" in st.load_warning
    aside = list(tmp_path.glob("schedules.json.broken-*"))
    assert len(aside) == 1 and json.loads(aside[0].read_text())["version"] == 2
    assert not p.exists()


def test_store_falls_back_to_bak(tmp_path):
    p = tmp_path / "schedules.json"
    write_json_atomic(p, {"version": 1, "schedules": {"s7f3a": raw(weekly())}})
    write_json_atomic(p, {"version": 1, "schedules": {}})
    p.write_text("{torn")
    st = ScheduleStore(p)
    st.load()
    assert set(st.all()) == {"s7f3a"} and "unusable" in st.load_warning


def test_latest_timestamp(tmp_path):
    st = ScheduleStore(tmp_path / "s.json")
    st.load()
    assert st.latest_timestamp() is None
    later = datetime(2026, 10, 11, 23, 0, tzinfo=SGT)
    st._parsed = {"s7f3a": weekly(last_fired=sm.LastFired(later, "x", "ok")), "t91c2": timer()}
    assert st.latest_timestamp() == later.timestamp()


async def test_mutate_rejects_schedules_that_would_not_load(tmp_path):
    st = ScheduleStore(tmp_path / "schedules.json")
    st.load()
    naive = datetime(2026, 10, 10, 12, 0)
    for bad in (weekly(label=" Bed "), weekly(updated_at=naive), weekly("t7f3a"),
                timer(when=sm.Once(datetime(2026, 10, 11, 1, 30, 0, 5, tzinfo=SGT)))):
        with pytest.raises(sm.ScheduleError):
            await st.mutate(lambda d, b=bad: d.update({b.id: b}))
    assert st.all() == {} and not (tmp_path / "schedules.json").exists()


async def test_mutate_refuses_broken_ids(tmp_path):
    p = tmp_path / "schedules.json"
    bad = raw(weekly())
    bad["rev"] = 0
    write_json_atomic(p, {"version": 1, "schedules": {"s7f3a": bad}})
    st = ScheduleStore(p)
    st.load()
    assert "s7f3a" in st.broken and "s7f3a" in st.ids()
    with pytest.raises(sm.ScheduleError, match="broken"):
        await st.mutate(lambda d: d.update({"s7f3a": weekly()}))
    assert json.loads(p.read_text())["schedules"]["s7f3a"] == bad


async def test_cancelled_mutate_does_not_lose_the_next_one(tmp_path, monkeypatch):
    import asyncio

    import iotbot.store as store_mod
    st = ScheduleStore(tmp_path / "schedules.json")
    st.load()
    gate = asyncio.Event()
    real = store_mod.write_json_atomic

    def slow_write(*a, **kw):
        import time as t
        while not gate.is_set():
            t.sleep(0.001)
        real(*a, **kw)

    monkeypatch.setattr(store_mod, "write_json_atomic", slow_write)
    task = asyncio.create_task(st.mutate(lambda d: d.update({"s0001": weekly("s0001")})))
    await asyncio.sleep(0.01)
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert set(st.all()) == {"s0001"}           # the write finished, so memory follows
    await st.mutate(lambda d: d.update({"s0002": weekly("s0002")}))
    assert set(json.loads((tmp_path / "schedules.json").read_text())["schedules"]) == {"s0001", "s0002"}


@pytest.mark.parametrize("main", ['{"version": 2, "schedules": {}}', None])
async def test_unusable_bak_is_moved_aside_too(tmp_path, main):
    p = tmp_path / "schedules.json"
    if main is not None:
        p.write_text(main)
    (tmp_path / "schedules.json.bak").write_text('{"version": 2, "schedules": {"keep": 1}}')
    st = ScheduleStore(p)
    st.load()
    assert "starting with no schedules" in st.load_warning
    await st.mutate(lambda d: d.update({"s7f3a": weekly()}))
    await st.mutate(lambda d: d.update({"s0002": weekly("s0002")}))
    kept = list(tmp_path.glob("schedules.json.bak.broken-*"))
    assert len(kept) == 1 and json.loads(kept[0].read_text())["schedules"] == {"keep": 1}
    assert len(list(tmp_path.glob("schedules.json.broken-*"))) == (main is not None)


async def test_latest_timestamp_survives_timer_deletion(tmp_path):
    st = ScheduleStore(tmp_path / "schedules.json")
    st.load()
    later = datetime(2030, 1, 1, tzinfo=SGT)
    await st.mutate(lambda d: d.update({"t91c2": timer(updated_at=later)}))
    await st.mutate(lambda d: d.pop("t91c2"))
    assert st.all() == {}
    assert st.latest_timestamp() is not None and st.latest_timestamp() >= datetime.now(SGT).timestamp() - 5
