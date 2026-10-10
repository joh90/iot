import asyncio
import dataclasses
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from iotbot.ac import mitsubishi as m
from iotbot.ac.state import AcState
from iotbot.devices.hub import HubError
from iotbot.schedule import model as sm
from iotbot.schedule.runner import GRACE_S, RETRY_DELAYS_S, Scheduler
from iotbot.schedule.store import ScheduleStore
from tests.test_ac_service import ALICE, BEDROOM, frames_of, make_ctx

SGT = ZoneInfo("Asia/Singapore")
T0 = datetime(2026, 10, 10, 12, 0, tzinfo=SGT)          # Sat noon; schedules were made then
FIRE = datetime(2026, 10, 10, 23, 0, tzinfo=SGT)
COOL24 = AcState(power=True, temp=24, fan=2, vane="swing")


class Clock:
    def __init__(self, at):
        self.t = at.timestamp()

    def __call__(self):
        return self.t

    def set(self, at):
        self.t = at.timestamp()


def sched(sid="s7f3a", **kw):
    base = dict(id=sid, device="bed_ac", action=sm.On(COOL24), when=sm.make_weekly("23:00", list(sm.DAYS)),
                created_by=1, created_at=T0, updated_by=1, updated_at=T0)
    return sm.Schedule(**{**base, **kw})


@pytest.fixture
async def env(tmp_path):
    ctx = make_ctx(tmp_path)
    store = ScheduleStore(tmp_path / "state" / "schedules.json")
    store.load()
    clock = Clock(T0)
    notes, sleeps = [], []

    async def notify(o):
        notes.append(o)

    async def sleep(s):
        sleeps.append(s)

    sch = Scheduler(store, ctx.devices, SGT, notify, clock=clock, sleep=sleep, synced=lambda: None)

    async def add(*schedules):
        await store.mutate(lambda d: d.update({s.id: s for s in schedules}))

    async def tick(at=None):
        if at:
            clock.set(at)
        await asyncio.gather(*await sch.tick())

    return dataclasses.make_dataclass("Env", ["ctx", "store", "clock", "sch", "notes", "sleeps", "add", "tick"])(
        ctx, store, clock, sch, notes, sleeps, add, tick)


def sent_states(ctx):
    return [m.decode(frames_of(p[0])[0]) for _, p, _ in ctx.sent]


async def test_fires_on_time_once(env):
    await env.add(sched())
    await env.tick(FIRE - timedelta(seconds=1))
    assert env.ctx.sent == []
    await env.tick(FIRE + timedelta(seconds=3))
    assert sent_states(env.ctx) == [COOL24]
    (o,) = env.notes
    assert o.result == "ok" and o.key == "s7f3a@2026-10-10T23:00:00+08:00" and o.prev_state is None
    lf = env.store.get("s7f3a").last_fired
    assert (lf.result, lf.occurrence) == ("ok", o.key)
    await env.tick(FIRE + timedelta(seconds=30))
    assert len(env.ctx.sent) == 1                          # never twice


async def test_restart_does_not_refire(env):
    await env.add(sched())
    await env.tick(FIRE + timedelta(seconds=1))
    again = Scheduler(env.store, env.ctx.devices, SGT, clock=env.clock, synced=lambda: None)
    env.clock.set(FIRE + timedelta(seconds=40))
    await asyncio.gather(*await again.tick())
    assert len(env.ctx.sent) == 1


async def test_clock_jump_back_does_not_refire(env):
    await env.add(sched())
    await env.tick(FIRE + timedelta(seconds=1))
    await env.tick(FIRE - timedelta(minutes=30))
    await env.tick(FIRE + timedelta(seconds=5))
    assert len(env.ctx.sent) == 1


async def test_late_is_missed_not_sent(env):
    await env.add(sched())
    await env.tick(FIRE + timedelta(seconds=GRACE_S + 1))
    assert env.ctx.sent == []
    (o,) = env.notes
    assert o.result == "missed" and o.missed_count == 1
    assert env.store.get("s7f3a").last_fired.result == "missed"
    await env.tick(FIRE + timedelta(minutes=10))
    assert len(env.notes) == 1                              # reported once


async def test_down_for_days_then_on_time(env):
    await env.add(sched())
    await env.tick(FIRE + timedelta(days=2, seconds=10))   # Mon 23:00:10; Sat + Sun missed
    assert sent_states(env.ctx) == [COOL24]
    missed, ok = env.notes
    assert missed.result == "missed" and missed.missed_count == 2 and missed.at == FIRE + timedelta(days=1)
    assert ok.result == "ok"


async def test_skip_date_consumed_and_old_ones_pruned(env):
    await env.add(sched(skip_dates=(date(2026, 10, 1), date(2026, 10, 10), date(2026, 10, 12))))
    await env.tick(FIRE + timedelta(seconds=1))
    assert env.ctx.sent == [] and env.notes[0].result == "skipped"
    s = env.store.get("s7f3a")
    assert s.skip_dates == (date(2026, 10, 12),) and s.last_fired.result == "skipped"
    await env.tick(FIRE + timedelta(days=1, seconds=1))
    assert len(env.ctx.sent) == 1


async def test_paused_and_disabled_do_nothing(env):
    await env.add(sched(), sched("s0002", enabled=False), sched("s0003", paused_until=FIRE + timedelta(hours=1)))
    await env.tick(FIRE + timedelta(seconds=1))
    assert len(env.ctx.sent) == 1 and [o.schedule.id for o in env.notes] == ["s7f3a"]
    assert env.store.get("s0002").last_fired is None and env.store.get("s0003").last_fired is None


async def test_timer_fires_then_is_deleted(env):
    t = sched("t0001", action=sm.Off(), when=sm.Once(FIRE))
    await env.add(t)
    await env.tick(FIRE + timedelta(seconds=1))
    assert sent_states(env.ctx) == [BEDROOM.with_changes(power=False)]   # Off = preset when nothing sent yet
    assert env.store.get("t0001") is None and env.notes[0].result == "ok"


async def test_missed_timer_is_reported_and_deleted(env):
    await env.add(sched("t0001", action=sm.Off(), when=sm.Once(FIRE)))
    await env.tick(FIRE + timedelta(hours=3))
    assert env.ctx.sent == [] and env.notes[0].result == "missed" and env.store.get("t0001") is None


async def test_adjust_needs_a_known_running_ac(env):
    adj = sched(action=sm.make_adjust({"temp": 26}))
    await env.add(adj)
    await env.tick(FIRE + timedelta(seconds=1))
    assert env.ctx.sent == [] and env.notes[-1].result == "skipped" and "does not know" in env.notes[-1].reason
    await env.ctx.devices.run("bed_ac", "power_off", ALICE)
    await env.tick(FIRE + timedelta(days=1, seconds=1))
    assert len(env.ctx.sent) == 1 and env.notes[-1].reason == "the AC is off"
    await env.ctx.devices.run("bed_ac", "power_on", ALICE)
    await env.tick(FIRE + timedelta(days=2, seconds=1))
    assert sent_states(env.ctx)[-1] == BEDROOM.with_changes(temp=26)
    assert env.notes[-1].result == "ok" and env.notes[-1].prev_state == BEDROOM


async def test_capture_on_unmanaged_device(env):
    await env.add(sched(device="old_ac", action=sm.Capture("power_on")))
    await env.tick(FIRE + timedelta(seconds=1))
    assert env.notes[-1].result == "ok" and len(env.ctx.sent) == 1


async def test_on_for_unmanaged_device_fails_cleanly(env):
    await env.add(sched(device="old_ac"), sched("s0002", device="gone"),
                  sched("s0003", device="old_ac", action=sm.Capture("nope")))
    await env.tick(FIRE + timedelta(seconds=1))
    reasons = {o.schedule.id: o.reason for o in env.notes}
    assert "AC encoder" in reasons["s7f3a"] and "no longer exists" in reasons["s0002"]
    assert "no 'nope'" in reasons["s0003"] and env.ctx.sent == []


async def test_retries_until_sent(env):
    fails = [HubError("offline"), HubError("offline")]
    real = env.ctx.hub.send_ir

    async def flaky(mac, packets, gap_s=0.0, idempotent=False):
        if fails:
            raise fails.pop(0)
        await real(mac, packets, gap_s, idempotent)

    env.ctx.hub.send_ir = flaky
    await env.add(sched())
    await env.tick(FIRE + timedelta(seconds=1))
    o = env.notes[-1]
    assert o.result == "ok" and o.attempts == 3 and env.sleeps == list(RETRY_DELAYS_S[:2])


async def test_gives_up_after_three_retries(env):
    env.ctx.fail = HubError("offline")
    await env.add(sched())
    await env.tick(FIRE + timedelta(seconds=1))
    o = env.notes[-1]
    assert o.result == "failed" and o.attempts == 1 + len(RETRY_DELAYS_S) and "offline" in o.reason
    assert env.store.get("s7f3a").last_fired.result == "failed"


async def test_newer_action_cancels_retry(env):
    env.ctx.fail = HubError("offline")

    async def sleep(s):
        env.ctx.fail = None
        await env.ctx.devices.run("bed_ac", "power_off", ALICE)    # someone presses Off meanwhile

    env.sch._sleep = sleep
    await env.add(sched())
    await env.tick(FIRE + timedelta(seconds=1))
    o = env.notes[-1]
    assert o.result == "superseded" and "Alice" in o.reason
    assert sent_states(env.ctx) == [BEDROOM.with_changes(power=False)]


async def test_toggle_not_resent_when_maybe_delivered(env):
    dev = env.ctx.registry.devices["old_ac"]
    dev.features["power_on"] = dataclasses.replace(dev.features["power_on"], idempotent=False)
    env.ctx.fail = HubError("timeout", maybe_delivered=True)
    await env.add(sched(device="old_ac", action=sm.Capture("power_on")))
    await env.tick(FIRE + timedelta(seconds=1))
    assert env.notes[-1].result == "failed" and env.notes[-1].attempts == 1 and env.sleeps == []


async def test_started_marker_is_saved_before_sending(env):
    seen = []
    real = env.ctx.hub.send_ir

    async def spy(*a, **kw):
        seen.append(env.store.get("s7f3a").last_fired.result)
        await real(*a, **kw)

    env.ctx.hub.send_ir = spy
    await env.add(sched())
    await env.tick(FIRE + timedelta(seconds=1))
    assert seen == ["started"]


async def test_interrupted_send_reported_after_restart(env):
    key = "s7f3a@2026-10-10T23:00:00+08:00"
    await env.add(sched(last_fired=sm.LastFired(FIRE, key, "started")),
                  sched("t0001", when=sm.Once(FIRE), last_fired=sm.LastFired(FIRE, "t0001@x", "started")))
    env.clock.set(FIRE + timedelta(minutes=1))
    await env.sch.recover_started()
    assert {o.schedule.id: o.result for o in env.notes} == {"s7f3a": "failed", "t0001": "failed"}
    assert env.store.get("s7f3a").last_fired.result == "failed" and env.store.get("t0001") is None
    await env.tick()
    assert env.ctx.sent == []


async def test_clock_guard_waits_for_ntp(env):
    await env.add(sched(updated_at=datetime(2026, 10, 10, 22, 0, tzinfo=SGT)))
    env.clock.set(datetime(2016, 1, 1, tzinfo=SGT))           # Pi booted without NTP
    ticks = []

    async def sleep(s):
        ticks.append(s)
        if len(ticks) == 3:
            env.clock.set(FIRE)

    env.sch._sleep = sleep
    await env.sch.wait_for_sane_clock()
    assert len(ticks) == 3 and env.clock() == FIRE.timestamp()


async def test_clock_guard_uses_extra_floor(env):
    env.sch._floor = lambda: FIRE.timestamp()
    assert env.sch.clock_floor() >= FIRE.timestamp()


async def test_seconds_to_next(env):
    assert env.sch.seconds_to_next() == 60
    await env.add(sched())
    env.clock.set(FIRE - timedelta(seconds=10))
    assert env.sch.seconds_to_next() == 10


async def test_run_forever_fires_and_stops(env):
    await env.add(sched())
    env.clock.set(FIRE)
    env.sch._sleep = asyncio.sleep
    env.sch.start()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if env.notes:
            break
    await env.sch.stop()
    assert env.notes and env.notes[0].result == "ok"


async def test_edit_does_not_resurrect_past_runs(env):
    # Created at noon for 11:00: today's 11:00 is before updated_at, so never "missed"
    await env.add(sched(when=sm.make_weekly("11:00", list(sm.DAYS))))
    await env.tick(T0 + timedelta(minutes=5))
    assert env.notes == [] and env.ctx.sent == []


# ---- review fixes -----------------------------------------------------------------------

async def test_adjust_cannot_undo_an_off_in_flight(env):
    await env.ctx.devices.run("bed_ac", "power_on", ALICE)
    gate = asyncio.Event()
    real = env.ctx.hub.send_ir

    async def slow(mac, packets, gap_s=0.0, idempotent=False):
        if not gate.is_set():
            gate.set()
            await asyncio.sleep(0.02)                       # Alice's Off is slow to send
        await real(mac, packets, gap_s, idempotent)

    env.ctx.hub.send_ir = slow
    await env.add(sched(action=sm.make_adjust({"temp": 26})))
    env.ctx.sent.clear()
    off = asyncio.create_task(env.ctx.devices.run("bed_ac", "power_off", ALICE))
    await gate.wait()
    await env.tick(FIRE + timedelta(seconds=1))
    await off
    assert [st.power for st in sent_states(env.ctx)] == [False]   # never back on
    assert env.notes[-1].result == "skipped" and env.notes[-1].reason == "the AC is off"


async def test_unsaved_started_marker_means_no_send(env, monkeypatch):
    await env.add(sched())

    async def boom(fn):
        raise OSError("SD card error")

    monkeypatch.setattr(env.store, "mutate", boom)
    await env.tick(FIRE + timedelta(seconds=1))
    assert env.ctx.sent == [] and env.notes[-1].result == "failed" and "not sent" in env.notes[-1].reason


async def test_clock_guard_trusts_ntp(env):
    await env.add(sched(updated_at=datetime(2030, 1, 1, tzinfo=SGT)))   # bogus future save
    env.sch._synced = lambda: True
    await env.sch.wait_for_sane_clock()                                  # does not block forever
    assert env.sleeps == []


async def test_clock_guard_waits_for_ntp_sync(env):
    states = [False, False, True]
    env.sch._synced = lambda: states.pop(0)
    await env.sch.wait_for_sane_clock()
    assert len(env.sleeps) == 2 and env.sch.clock_state == "waiting for NTP"


async def test_clock_guard_gives_up_on_ntp_if_clock_is_past_floor(env):
    env.sch._synced = lambda: False
    await env.sch.wait_for_sane_clock()
    assert sum(env.sleeps) >= 30 * 60


async def test_clock_guard_unsynced_and_behind_keeps_waiting(env):
    await env.add(sched())
    env.clock.set(datetime(2016, 1, 1, tzinfo=SGT))
    env.sch._synced = lambda: False

    async def sleep(s):
        env.sleeps.append(s)
        if len(env.sleeps) == 200:
            env.clock.set(FIRE)

    env.sch._sleep = sleep
    await env.sch.wait_for_sane_clock()
    assert len(env.sleeps) >= 200


def test_ntp_synced_reads_the_kernel():
    from iotbot.schedule.runner import ntp_synced
    assert ntp_synced() in (True, False, None)


async def test_forward_jump_is_missed_not_fired(env):
    await env.add(sched())
    await env.tick(FIRE - timedelta(hours=1))
    await env.tick(FIRE + timedelta(hours=5))              # clock jumped (or the Pi stalled)
    assert env.ctx.sent == [] and env.notes[-1].result == "missed"


async def test_disabled_timer_past_its_time_is_swept(env):
    await env.add(sched("t0001", action=sm.Off(), when=sm.Once(FIRE), enabled=False))
    await env.tick(FIRE + timedelta(seconds=1))
    assert env.store.get("t0001") is not None              # within grace, could be re-enabled
    await env.tick(FIRE + timedelta(minutes=5))
    assert env.store.get("t0001") is None and env.notes == [] and env.ctx.sent == []


async def test_failed_final_save_is_retried(env, monkeypatch):
    await env.add(sched("t0001", action=sm.Off(), when=sm.Once(FIRE)), sched())
    real = env.store.mutate
    calls = []

    async def flaky(fn):
        calls.append(1)
        if len(calls) in (3, 4):          # both final saves (after the two "started" ones) fail
            raise OSError("SD card error")
        return await real(fn)

    monkeypatch.setattr(env.store, "mutate", flaky)
    await env.tick(FIRE + timedelta(seconds=1))
    assert len(env.ctx.sent) == 2 and {o.result for o in env.notes} == {"ok"}
    assert env.store.get("t0001").last_fired.result == "started"
    await env.tick(FIRE + timedelta(seconds=30))
    assert env.store.get("t0001") is None and env.store.get("s7f3a").last_fired.result == "ok"
    assert len(env.ctx.sent) == 2 and env.sch._unsaved == {}


async def test_sweep_spares_a_timer_edited_meanwhile(env):
    await env.add(sched("t0001", action=sm.Off(), when=sm.Once(FIRE), enabled=False))
    env.clock.set(FIRE + timedelta(minutes=5))
    later = sm.Once(FIRE + timedelta(days=1))
    real = env.store.mutate

    async def edit_first(fn):
        await real(lambda d: d.update({"t0001": dataclasses.replace(d["t0001"], enabled=True, when=later)}))
        return await real(fn)

    env.store.mutate = edit_first
    await env.sch.sweep_timers(env.sch.now())
    env.store.mutate = real
    assert env.store.get("t0001").when == later


async def test_bad_schedule_does_not_stop_the_tick(env, monkeypatch):
    from iotbot.schedule import runner
    await env.add(sched(), sched("s0002", device="office_ac"))
    real = runner.tm.due

    def due(s, *a):
        if s.id == "s7f3a":
            raise RuntimeError("bad")
        return real(s, *a)

    monkeypatch.setattr(runner.tm, "due", due)
    await env.tick(FIRE + timedelta(seconds=1))
    assert [o.schedule.id for o in env.notes] == ["s0002"]


async def test_loop_survives_errors(env, monkeypatch):
    calls = []

    def bad():
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(env.sch, "seconds_to_next", bad)
    env.sch._sleep = asyncio.sleep
    env.sch.start()
    await asyncio.sleep(0.05)
    assert calls and not env.sch._task.done()
    await env.sch.stop()


async def test_wake_after_edit_fires_promptly(env):
    env.clock.set(FIRE - timedelta(seconds=1))
    env.sch._sleep = asyncio.sleep
    env.sch.start()
    await asyncio.sleep(0.02)                              # loop now sleeping up to 60s
    await env.add(sched(when=sm.make_weekly("23:00", ["sat"])))
    env.clock.set(FIRE + timedelta(seconds=1))
    env.sch.wake()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if env.notes:
            break
    await env.sch.stop()
    assert env.notes and env.notes[0].result == "ok"


async def test_seconds_to_next_with_pause(env):
    await env.add(sched(paused_until=FIRE + timedelta(days=3)))
    env.clock.set(FIRE - timedelta(seconds=10))
    assert env.sch.seconds_to_next() == 60


async def test_prev_state_comes_from_inside_the_lock(env):
    await env.ctx.devices.run("bed_ac", "power_on", ALICE)
    await env.add(sched())
    await env.tick(FIRE + timedelta(seconds=1))
    assert env.notes[-1].prev_state == BEDROOM


async def test_error_in_one_schedule_spares_the_rest(env, monkeypatch):
    from iotbot.schedule import runner
    await env.add(sched(), sched("s0002", device="office_ac"))
    real = runner.tm.blocked

    def blocked(s, *a):
        if s.id == "s7f3a":
            raise RuntimeError("bad")
        return real(s, *a)

    monkeypatch.setattr(runner.tm, "blocked", blocked)
    await env.tick(FIRE + timedelta(seconds=1))
    assert [o.schedule.id for o in env.notes] == ["s0002"]


async def test_synced_clock_with_future_saves_is_reported(env):
    await env.add(sched(updated_at=datetime(2030, 1, 1, tzinfo=SGT)))
    env.sch._synced = lambda: True
    await env.sch.wait_for_sane_clock()
    assert env.sch.clock_state.startswith("saved times are ahead")


async def test_execute_without_retries_for_people(env):
    env.ctx.fail = HubError("offline")
    s = sched()
    o = await env.sch.execute(s, FIRE, "k", actor=ALICE, retries=False)
    assert o.result == "failed" and o.attempts == 1 and env.sleeps == []
