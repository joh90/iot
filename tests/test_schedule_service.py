import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from iotbot.result import Actor
from iotbot.schedule import model as sm
from iotbot.schedule.service import MAX_TIMERS_PER_DEVICE, MAX_WEEKLY, ScheduleService
from iotbot.schedule.store import ScheduleStore
from iotbot.store import JsonlLog
from tests.test_ac_service import make_ctx

SGT = ZoneInfo("Asia/Singapore")
T0 = datetime(2026, 10, 10, 12, 0, tzinfo=SGT)            # Sat noon
ALICE = Actor(1, "Alice", "button")
BOB = Actor(2, "Bob", "slash")
ON22 = {"kind": "on", "state": {"power": True, "temp": 22, "fan": 3, "vane": "auto"}}
WEEKLY = {"kind": "weekly", "time": "23:00", "days": ["mon", "tue", "wed", "thu", "sun"]}


@pytest.fixture
def svc(tmp_path):
    ctx = make_ctx(tmp_path)
    store = ScheduleStore(tmp_path / "state" / "schedules.json")
    store.load()
    clock = [T0.timestamp()]
    woke = []
    s = ScheduleService(store, ctx.devices, JsonlLog(tmp_path / "logs", "schedule_events"), SGT,
                        clock=lambda: clock[0], on_change=lambda: woke.append(1))
    s.clock, s.woke, s.tmp = clock, woke, tmp_path
    return s


async def create(svc, actor=ALICE, resolve=None, **spec):
    spec = {"device": "bed_ac", "action": ON22, "when": WEEKLY, **spec}
    p = svc.plan(actor, spec)
    assert p.ok, p.message
    r = await svc.apply(actor, p.data["plan_id"], resolve)
    assert r.ok, r.message
    return r.data["schedule"]


def events(svc):
    return [json.loads(line) for f in sorted((svc.tmp / "logs").glob("schedule_events*"))
            for line in f.read_text().splitlines()]


async def test_plan_previews_then_apply_saves(svc):
    p = svc.plan(ALICE, {"device": "bed_ac", "action": ON22, "when": WEEKLY, "label": " Bed  time "})
    assert p.ok and p.data["next_runs"][:2] == ["2026-10-11T23:00:00+08:00", "2026-10-12T23:00:00+08:00"]
    assert p.message.startswith("Bed time: bed_ac on, cool 22C fan 3 vane auto, 23:00 Sun-Thu")
    assert "Next: tomorrow 23:00, Mon 12 Oct 23:00" in p.message
    assert svc.store.all() == {} and svc.woke == []
    r = await svc.apply(ALICE, p.data["plan_id"])
    s = r.data["schedule"]
    assert r.ok and svc.get(s.id) == s and s.rev == 1 and s.created_by == 1 and svc.woke == [1]
    ev = events(svc)[-1]
    assert (ev["event"], ev["sid"], ev["before"], ev["after"]["id"]) == ("created", s.id, None, s.id)
    assert (await svc.apply(ALICE, p.data["plan_id"])).error == "plan_expired"    # single use


async def test_plan_expires_and_belongs_to_its_user(svc):
    p = svc.plan(ALICE, {"device": "bed_ac", "action": ON22, "when": WEEKLY})
    assert (await svc.apply(BOB, p.data["plan_id"])).error == "forbidden"
    svc.clock[0] += 601
    assert (await svc.apply(ALICE, p.data["plan_id"])).error == "plan_expired"


@pytest.mark.parametrize("spec,error", [
    ({"device": "nope"}, "device_not_found"),
    ({"action": {"kind": "on", "state": {"power": True, "temp": 40}}}, "invalid"),
    ({"when": {"kind": "weekly", "time": "25:00", "days": ["mon"]}}, "invalid"),
    ({"device": "old_ac"}, "not_supported"),                        # On needs the AC encoder
    ({"action": {"kind": "capture", "key": "nope"}}, "not_supported"),
    ({"when": {"kind": "once", "at": "2026-10-10T11:00:00+08:00"}}, "in_the_past"),
    ({"colour": "red"}, "invalid"),
    ({"label": 5}, "invalid"),
])
def test_plan_rejects(svc, spec, error):
    r = svc.plan(ALICE, {"device": "bed_ac", "action": ON22, "when": WEEKLY, **spec})
    assert not r.ok and r.error == error, r.message


async def test_labels_unique_per_device(svc):
    await create(svc, label="Bedtime")
    r = svc.plan(ALICE, {"device": "bed_ac", "action": ON22, "label": "BEDTIME",
                         "when": {"kind": "weekly", "time": "07:00", "days": ["mon"]}})
    assert r.error == "label_taken"
    assert svc.plan(ALICE, {"device": "office_ac", "action": ON22, "when": WEEKLY, "label": "Bedtime"}).ok


async def test_clash_needs_a_choice(svc):
    first = await create(svc, actor=BOB)
    spec = {"device": "bed_ac", "action": {"kind": "off"},
            "when": {"kind": "weekly", "time": "23:03", "days": ["sun"]}}
    p = svc.plan(ALICE, spec)
    (c,) = p.conflicts
    assert c["with"] == first.id and c["owner"] == 2 and c["at"] == "2026-10-11T23:03:00+08:00"
    r = await svc.apply(ALICE, p.data["plan_id"])
    assert r.error == "conflict" and r.conflicts and svc.get(first.id)
    r = await svc.apply(ALICE, p.data["plan_id"], "keep_both")
    assert r.ok and len(svc.store.all()) == 2


async def test_clash_replace_deletes_the_other(svc):
    first = await create(svc, actor=BOB)
    p = svc.plan(ALICE, {"device": "bed_ac", "action": ON22,
                         "when": {"kind": "weekly", "time": "23:00", "days": ["sun"]}})
    r = await svc.apply(ALICE, p.data["plan_id"], "replace")
    assert r.ok and svc.get(first.id) is None and "Replaced" in r.message
    assert r.data["notify"] == [2]                                  # Bob's schedule was removed
    assert [e["event"] for e in events(svc)][-2:] == ["deleted", "created"]


async def test_other_devices_and_far_apart_runs_do_not_clash(svc):
    await create(svc)
    assert not svc.plan(ALICE, {"device": "office_ac", "action": ON22, "when": WEEKLY}).conflicts
    assert not svc.plan(ALICE, {"device": "bed_ac", "action": {"kind": "off"},
                                "when": {"kind": "weekly", "time": "23:06", "days": ["sun"]}}).conflicts


async def test_never_off_warning(svc):
    p = svc.plan(ALICE, {"device": "bed_ac", "action": ON22, "when": WEEKLY})
    assert p.warnings and "Nothing turns bed_ac off" in p.warnings[0]
    await create(svc, action={"kind": "off"}, when={"kind": "weekly", "time": "07:00", "days": list(sm.DAYS)})
    assert not svc.plan(ALICE, {"device": "bed_ac", "action": ON22, "when": WEEKLY}).warnings


async def test_edit_with_rev(svc):
    s = await create(svc, actor=BOB)
    p = svc.plan(ALICE, {"when": {"kind": "weekly", "time": "22:30", "days": ["sun"]}}, sid=s.id, rev=1)
    assert p.ok
    r = await svc.apply(ALICE, p.data["plan_id"])
    new = r.data["schedule"]
    assert new.rev == 2 and new.when.time == "22:30" and new.action == s.action and new.created_by == 2
    assert r.data["notify"] == [2] and "Updated" in r.message
    stale = svc.plan(ALICE, {"label": "x"}, sid=s.id, rev=1)
    assert stale.error == "stale" and stale.data["schedule"].rev == 2


async def test_apply_refuses_if_changed_after_plan(svc):
    s = await create(svc)
    p = svc.plan(ALICE, {"label": "late"}, sid=s.id)
    await svc.pause(BOB, [s.id])
    r = await svc.apply(ALICE, p.data["plan_id"])
    assert r.error == "stale" and svc.get(s.id).label == ""


async def test_timer_kind_cannot_change(svc):
    s = await create(svc)
    r = svc.plan(ALICE, {"when": {"kind": "once", "at": "2026-10-10T23:00:00+08:00"}}, sid=s.id)
    assert r.error == "invalid" and "make a new one" in r.message


async def test_passed_today_is_reported(svc):
    p = svc.plan(ALICE, {"device": "bed_ac", "action": ON22,
                         "when": {"kind": "weekly", "time": "11:30", "days": ["sat"]}})
    assert p.data["passed_today"] == "2026-10-10T11:30:00+08:00"
    assert p.data["next_runs"][0] == "2026-10-17T11:30:00+08:00"          # no silent catch-up


async def test_limits(svc):
    for i in range(MAX_TIMERS_PER_DEVICE):
        at = (T0 + timedelta(hours=1, minutes=10 * i)).isoformat()
        await create(svc, action={"kind": "off"}, when={"kind": "once", "at": at}, resolve="keep_both")
    r = svc.plan(ALICE, {"device": "bed_ac", "action": {"kind": "off"},
                         "when": {"kind": "once", "at": (T0 + timedelta(days=1)).isoformat()}})
    assert r.error == "limit"
    assert svc.plan(ALICE, {"device": "office_ac", "action": {"kind": "off"},
                            "when": {"kind": "once", "at": (T0 + timedelta(days=1)).isoformat()}}).ok
    for i in range(MAX_WEEKLY):
        await create(svc, device="office_ac", when={"kind": "weekly", "time": f"{i // 6:02d}:{i % 6 * 10:02d}",
                                                    "days": ["mon"]}, resolve="keep_both")
    assert svc.plan(ALICE, {"device": "bed_ac", "action": ON22, "when": WEEKLY}).error == "limit"


async def test_delete_and_revert(svc):
    s = await create(svc)
    r = await svc.delete(BOB, s.id, rev=1)
    assert r.ok and svc.get(s.id) is None and r.data["notify"] == [1]
    back = await svc.revert(ALICE, r.data["event_id"])
    assert back.ok and svc.get(s.id).when == s.when and svc.get(s.id).rev == 2
    assert (await svc.delete(ALICE, "s0000")).error == "not_found"


async def test_revert_create_and_changed_since(svc):
    p = svc.plan(ALICE, {"device": "bed_ac", "action": ON22, "when": WEEKLY})
    r = await svc.apply(ALICE, p.data["plan_id"])
    sid, created = r.data["schedule"].id, r.data["event_id"]
    paused = await svc.pause(ALICE, [sid])
    assert (await svc.revert(ALICE, created)).error == "changed_since"
    assert (await svc.revert(ALICE, paused.data["event_id"])).ok and svc.get(sid).enabled
    assert (await svc.revert(ALICE, "e00000000")).error == "change_not_found"


async def test_pause_until_and_resume(svc):
    s = await create(svc)
    t = await create(svc, action={"kind": "off"}, when={"kind": "once", "at": "2026-10-11T01:00:00+08:00"})
    until = T0 + timedelta(days=3)
    r = await svc.pause(ALICE, svc.select("bed_ac"), until)
    assert r.ok and "until Tue 13 Oct 12:00" in r.message
    assert svc.get(s.id).paused_until == until and svc.get(s.id).enabled
    assert not svc.get(t.id).enabled                                    # timers are disabled instead
    assert (await svc.pause(ALICE, [s.id], T0 - timedelta(hours=1))).error == "in_the_past"
    r = await svc.resume(ALICE, svc.select("all"))
    assert svc.get(s.id).paused_until is None and svc.get(t.id).enabled
    assert svc.select("zzz") == [] and (await svc.resume(ALICE, [])).error == "nothing_selected"


async def test_skip_next_and_specific(svc):
    s = await create(svc, when={"kind": "weekly", "time": "01:00", "days": ["sat", "sun"]})
    r = await svc.skip(ALICE, s.id)
    assert r.ok and svc.get(s.id).skip_dates == (date(2026, 10, 11),) and "Sat 10 Oct night (Sun 01:00)" in r.message
    assert (await svc.skip(ALICE, s.id, date(2026, 10, 12))).error == "invalid"      # a Monday
    assert (await svc.skip(ALICE, s.id, date(2026, 10, 3))).error == "invalid"       # past
    await svc.skip(ALICE, s.id, date(2026, 10, 17))
    assert svc.get(s.id).skip_dates == (date(2026, 10, 11), date(2026, 10, 17))
    await svc.unskip(ALICE, s.id, date(2026, 10, 11))
    assert svc.get(s.id).skip_dates == (date(2026, 10, 17),)
    t = await create(svc, action={"kind": "off"}, when={"kind": "once", "at": "2026-10-11T01:00:00+08:00"},
                     resolve="keep_both")
    assert (await svc.skip(ALICE, t.id)).error == "invalid"


async def test_noop_edit_writes_nothing(svc):
    s = await create(svc)
    n = len(events(svc))
    r = await svc.resume(ALICE, [s.id])
    assert r.ok and r.data["ids"] == [] and len(events(svc)) == n and svc.get(s.id).rev == 1


async def test_problem_and_agenda(svc):
    s = await create(svc)
    assert svc.problem(s) is None
    svc.devices.registry.devices.pop("bed_ac")
    assert "no longer exists" in svc.problem(s)
    ag = svc.agenda(T0, T0 + timedelta(days=2))
    assert [(a.isoformat(), x.id, why) for a, x, why in ag] == [("2026-10-11T23:00:00+08:00", s.id, "broken")]


async def test_list_sorted_by_next_run(svc):
    late = await create(svc, when={"kind": "weekly", "time": "23:30", "days": ["sat"]})
    soon = await create(svc, device="office_ac", when={"kind": "weekly", "time": "13:00", "days": ["sat"]})
    off = await create(svc, device="office_ac", when={"kind": "weekly", "time": "14:00", "days": ["sat"]})
    await svc.pause(ALICE, [off.id])
    assert [s.id for s in svc.list()] == [soon.id, late.id, off.id]
    assert [s.id for s in svc.list("bed_ac")] == [late.id]


async def test_store_failure_reported(svc, monkeypatch):
    import iotbot.store as store_mod

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(store_mod, "write_json_atomic", boom)
    p = svc.plan(ALICE, {"device": "bed_ac", "action": ON22, "when": WEEKLY})
    r = await svc.apply(ALICE, p.data["plan_id"])
    assert r.error == "store_error" and svc.store.all() == {} and not events(svc)


# ---- review fixes -----------------------------------------------------------------------

async def test_replace_only_deletes_what_was_shown(svc):
    p = svc.plan(ALICE, {"device": "bed_ac", "action": ON22, "when": WEEKLY})
    assert not p.conflicts
    bob = await create(svc, actor=BOB, action={"kind": "off"},
                       when={"kind": "weekly", "time": "23:00", "days": ["sun"]})
    r = await svc.apply(ALICE, p.data["plan_id"], "replace")
    assert r.error == "conflict" and r.conflicts[0]["with"] == bob.id and svc.get(bob.id)
    r = await svc.apply(ALICE, p.data["plan_id"], "replace")     # now it was shown
    assert r.ok and svc.get(bob.id) is None


async def test_replace_refused_if_the_clash_changed(svc):
    bob = await create(svc, actor=BOB)
    p = svc.plan(ALICE, {"device": "bed_ac", "action": {"kind": "off"},
                         "when": {"kind": "weekly", "time": "23:00", "days": ["sun"]}})
    await svc.pause(BOB, [bob.id], T0 + timedelta(hours=1))      # rev 2, still clashes
    r = await svc.apply(ALICE, p.data["plan_id"], "replace")
    assert r.error == "conflict" and svc.get(bob.id)


async def test_edit_keeps_what_the_scheduler_wrote(svc):
    s = await create(svc, when={"kind": "weekly", "time": "23:00", "days": ["sat", "sun"]})
    await svc.skip(ALICE, s.id)
    p = svc.plan(ALICE, {"label": "Bed"}, sid=s.id)
    lf = sm.LastFired(T0, f"{s.id}@2026-10-10T23:00:00+08:00", "started")
    from dataclasses import replace
    await svc.store.mutate(lambda d: d.update({s.id: replace(d[s.id], last_fired=lf, skip_dates=())}))
    r = await svc.apply(ALICE, p.data["plan_id"])
    new = svc.get(s.id)
    assert r.ok and new.label == "Bed" and new.last_fired == lf and new.skip_dates == ()


async def test_events_use_the_service_clock(svc):
    await create(svc)
    assert events(svc)[-1]["ts"].startswith("2026-10-10T12:00:00")


async def test_double_tap_skip_with_rev(svc):
    s = await create(svc)
    assert (await svc.skip(ALICE, s.id, rev=1)).ok
    assert (await svc.skip(ALICE, s.id, rev=1)).error == "stale"
    assert len(svc.get(s.id).skip_dates) == 1


async def test_revert_replace_brings_back_the_deleted(svc):
    bob = await create(svc, actor=BOB)
    p = svc.plan(ALICE, {"device": "bed_ac", "action": ON22,
                         "when": {"kind": "weekly", "time": "23:00", "days": ["sun"]}})
    r = await svc.apply(ALICE, p.data["plan_id"], "replace")
    new = r.data["schedule"]
    back = await svc.revert(ALICE, r.data["event_id"])
    assert back.ok and svc.get(bob.id) and svc.get(new.id) is None and back.data["notify"] == [2]


async def test_revert_bulk_pause(svc):
    a = await create(svc)
    b = await create(svc, device="office_ac")
    r = await svc.pause(ALICE, svc.select("all"))
    assert (await svc.revert(ALICE, r.data["event_id"])).ok
    assert svc.get(a.id).enabled and svc.get(b.id).enabled


async def test_revert_checks_labels(svc):
    s = await create(svc, label="Bed")
    d = await svc.delete(ALICE, s.id)
    await create(svc, label="Bed", when={"kind": "weekly", "time": "07:00", "days": ["mon"]})
    r = await svc.revert(ALICE, d.data["event_id"])
    assert r.error == "label_taken" and svc.get(s.id) is None


async def test_far_timer_is_checked(svc):
    at = T0 + timedelta(days=10, hours=11)
    await create(svc, action={"kind": "off"}, when={"kind": "once", "at": at.isoformat()})
    p = svc.plan(ALICE, {"device": "bed_ac", "action": ON22,
                         "when": {"kind": "once", "at": (at + timedelta(minutes=2)).isoformat()}})
    assert p.conflicts


async def test_never_off_checks_every_night(svc):
    await create(svc, action={"kind": "off"}, when={"kind": "weekly", "time": "07:00", "days": ["mon"]})
    p = svc.plan(ALICE, {"device": "bed_ac", "action": ON22,
                         "when": {"kind": "weekly", "time": "23:00", "days": list(sm.DAYS)}})
    assert p.warnings and "6 of its runs" in p.warnings[0]


async def test_replace_same_label(svc):
    old = await create(svc, label="Bed", actor=BOB)
    p = svc.plan(ALICE, {"device": "bed_ac", "action": ON22, "label": "bed",
                         "when": {"kind": "weekly", "time": "23:00", "days": ["sun"]}})
    assert p.ok and p.conflicts
    assert (await svc.apply(ALICE, p.data["plan_id"], "keep_both")).error == "label_taken"
    r = await svc.apply(ALICE, p.data["plan_id"], "replace")
    assert r.ok and svc.get(old.id) is None and r.data["schedule"].label == "bed"


async def test_passed_today_only_when_time_changes(svc):
    s = await create(svc, when={"kind": "weekly", "time": "11:00", "days": ["sat"]})
    assert svc.plan(ALICE, {"label": "x"}, sid=s.id).data["passed_today"] is None
    assert svc.plan(ALICE, {"when": {"kind": "weekly", "time": "11:30", "days": ["sat"]}},
                    sid=s.id).data["passed_today"] == "2026-10-10T11:30:00+08:00"


async def test_keep_both_returns_conflicts_for_notices(svc):
    await create(svc, actor=BOB)
    p = svc.plan(ALICE, {"device": "bed_ac", "action": {"kind": "off"},
                         "when": {"kind": "weekly", "time": "23:00", "days": ["sun"]}})
    r = await svc.apply(ALICE, p.data["plan_id"], "keep_both")
    assert r.data["kept_conflicts"][0]["owner"] == 2


async def test_timer_moved_to_full_device_hits_limit(svc):
    for i in range(10):
        at = (T0 + timedelta(hours=1, minutes=10 * i)).isoformat()
        await create(svc, device="office_ac", action={"kind": "off"}, when={"kind": "once", "at": at},
                     resolve="keep_both")
    t = await create(svc, action={"kind": "off"}, when={"kind": "once", "at": (T0 + timedelta(days=2)).isoformat()})
    assert svc.plan(ALICE, {"device": "office_ac"}, sid=t.id).error == "limit"


async def test_broken_entries_visible_and_deletable(svc):
    from iotbot.store import write_json_atomic
    bad = {"id": "s0bad"}
    write_json_atomic(svc.store.path, {"version": 1, "schedules": {"s0bad": bad}})
    svc.store.load()
    assert "s0bad" in svc.broken()
    assert (await svc.drop_broken(ALICE, "s0bad")).ok and svc.broken() == {}
    assert (await svc.drop_broken(ALICE, "s0bad")).error == "not_found"


def test_plans_capped_per_user(svc):
    for _ in range(30):
        svc.plan(ALICE, {"device": "bed_ac", "action": ON22, "when": WEEKLY})
    assert len(svc.plans) == 20


def test_text_helpers():
    from iotbot.schedule import text as tx
    assert tx.describe_days(("mon", "tue", "wed", "thu", "sun")) == "Sun-Thu"
    assert tx.describe_days(("mon", "tue", "fri", "sat", "sun")) == "Fri-Tue"
    assert tx.describe_days(("mon", "wed", "fri")) == "Mon, Wed, Fri"
    assert tx.describe_days(tuple(sm.DAYS)) == "every day"
    assert tx.describe_days(("sat", "sun")) == "weekends"
    assert tx.night_of(date(2026, 10, 17), "01:00") == "Fri 16 Oct night (Sat 01:00)"
    assert tx.night_of(date(2026, 10, 17), "23:00") == "Sat 17 Oct"
