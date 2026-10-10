from datetime import timedelta

import pytest

from iotbot.bot.callbacks import decode
from iotbot.devices.hub import HubError
from iotbot.result import Actor, Result
from iotbot.schedule import model as sm
from iotbot.schedule.notify import Notifier
from iotbot.schedule.runner import FireOutcome
from iotbot.schedule.service import ScheduleService
from iotbot.store import JsonlLog
from tests.test_ac_service import ALICE, BEDROOM
from tests.test_schedule_runner import COOL24, FIRE, SGT, env, sched  # noqa: F401 -- fixture


@pytest.fixture
def nt(env):  # noqa: F811
    svc = ScheduleService(env.store, env.ctx.devices, JsonlLog(env.ctx.settings.log_dir, "schedule_events"), SGT,
                          clock=env.clock)
    sent = []

    async def send(n):
        sent.append(n)

    approved = {1, 2}
    n = Notifier(svc, env.sch, lambda uid: [uid] if uid in approved else sorted(approved), send, clock=env.clock)
    env.sch.notify = n.on_fire
    n.sent, n.env = sent, env
    return n


def buttons(notice):
    return [decode(cb)[1] for row in notice.buttons for _, cb in row]


async def test_ok_is_silent_with_undo_skip_pause(nt):
    e = nt.env
    await e.ctx.devices.run("bed_ac", "power_on", ALICE)
    await e.add(sched(label="Bedtime"))
    await e.tick(FIRE + timedelta(seconds=1))
    (n,) = nt.sent
    assert n.user_id == 1 and n.silent and n.text == "Bedtime: bed_ac on, cool 24C fan 2 vane swing"
    assert buttons(n) == ["undo", "skip", "pause"]


async def test_undo_restores_previous_state(nt):
    e = nt.env
    await e.ctx.devices.run("bed_ac", "power_on", ALICE)
    await e.add(sched())
    await e.tick(FIRE + timedelta(seconds=1))
    tok = decode(nt.sent[0].buttons[0][0][1])[2]
    assert decode(nt.sent[0].buttons[0][1][1]) == ["sc", "skip", "s7f3a", "1"]
    r = await nt.undo(ALICE, tok)
    assert r.ok and "resent ON cool 22C" in r.message and e.ctx.ac.last("bed_ac").state == BEDROOM
    assert (await nt.undo(ALICE, tok)).error == "expired"               # single use


async def test_undo_refused_when_superseded_or_late(nt):
    e = nt.env
    await e.ctx.devices.run("bed_ac", "power_on", ALICE)
    await e.add(sched())
    await e.tick(FIRE + timedelta(seconds=1))
    tok = decode(nt.sent[0].buttons[0][0][1])[2]
    await e.ctx.devices.run("bed_ac", "power_off", ALICE)
    r = await nt.undo(ALICE, tok)
    assert r.error == "superseded" and "Alice" in r.message
    e.clock.t += 11 * 60
    assert (await nt.undo(ALICE, tok)).error == "expired"


async def test_no_undo_without_known_previous_state(nt):
    await nt.env.add(sched())
    await nt.env.tick(FIRE + timedelta(seconds=1))
    assert buttons(nt.sent[0]) == ["skip", "pause"]


async def test_timer_ok_has_only_undo(nt):
    e = nt.env
    await e.ctx.devices.run("bed_ac", "power_on", ALICE)
    await e.add(sched("t0001", action=sm.Off(), when=sm.Once(FIRE)))
    await e.tick(FIRE + timedelta(seconds=1))
    assert buttons(nt.sent[0]) == ["undo"]


async def test_failed_is_loud_with_retry(nt):
    e = nt.env
    e.ctx.fail = HubError("RM offline")
    await e.add(sched())
    await e.tick(FIRE + timedelta(seconds=1))
    (n,) = nt.sent
    assert not n.silent and n.text.startswith("FAILED s7f3a: bed_ac on") and "RM offline" in n.text
    assert buttons(n) == ["retry", "pause"]
    e.ctx.fail = None
    r = await nt.retry(ALICE, decode(n.buttons[0][0][1])[2])
    assert r.ok and e.ctx.ac.last("bed_ac").state == COOL24 and e.ctx.ac.last("bed_ac").actor == "Alice"


async def test_retry_failure_does_not_wait(nt):
    e = nt.env
    e.ctx.fail = HubError("RM offline")
    s = sched("t0001", when=sm.Once(FIRE))
    tok = nt._token("retry", s, 60)
    r = await nt.retry(ALICE, tok)
    assert r.error == "send_failed" and e.sleeps == []


async def test_missed_is_loud(nt):
    e = nt.env
    await e.add(sched())
    await e.tick(FIRE + timedelta(days=1, hours=2))
    (n,) = nt.sent
    assert not n.silent and n.text.startswith("Missed s7f3a") and "2 runs" in n.text and n.buttons == []


async def test_skip_by_request_says_nothing_but_ac_off_does(nt):
    o = FireOutcome(sched(), FIRE, "k", "skipped", "skipped by request")
    assert nt.fire_notices(o) == []
    (n,) = nt.fire_notices(FireOutcome(sched(), FIRE, "k", "skipped", "the AC is off"))
    assert n.silent and "Skipped: the AC is off" in n.text


async def test_unknown_creator_goes_to_everyone(nt):
    (a, b) = nt.fire_notices(FireOutcome(sched(created_by=99), FIRE, "k", "missed", missed_count=1))
    assert {a.user_id, b.user_id} == {1, 2}


async def test_text_is_escaped(nt):
    (n,) = nt.fire_notices(FireOutcome(sched(label="<b>x</b>"), FIRE, "k", "ok"))
    assert "&lt;b&gt;x&lt;/b&gt;" in n.text


async def test_changes_notify_creator_and_clash_owner(nt):
    r = Result.success("Paused bedtime", data={"notify": [2, 1], "kept_conflicts": [
        {"owner": 2, "message": "dup"}, {"owner": 3, "message": "x"}]})
    await nt.changed(Actor(1, "Alice", "button"), r)
    assert [(n.user_id, n.text.split(" ")[0]) for n in nt.sent] == [(2, "Alice"), (3, "Alice")]


async def test_report_problems(nt):
    e = nt.env
    await e.add(sched(device="old_ac"))
    await nt.report_problems()
    (n,) = nt.sent
    assert "cannot run" in n.text and "AC encoder" in n.text and not n.silent


async def test_send_errors_are_contained(nt):
    async def boom(n):
        raise RuntimeError("telegram down")

    nt.send = boom
    await nt.on_fire(FireOutcome(sched(), FIRE, "k", "missed", missed_count=1))


async def test_tokens_are_capped(nt):
    for _ in range(300):
        nt._token("retry", sched(), 60)
    assert len(nt.tokens) == 200


def _failed(nt):
    (n,) = nt.sent
    return decode(n.buttons[0][0][1])[2]


async def test_retry_is_single_use_and_checks_newer_actions(nt):
    e = nt.env
    e.ctx.fail = HubError("RM offline")
    await e.add(sched())
    await e.tick(FIRE + timedelta(seconds=1))
    tok = _failed(nt)
    e.ctx.fail = None
    await e.ctx.devices.run("bed_ac", "power_off", ALICE)
    r = await nt.retry(ALICE, tok)
    assert r.error == "superseded" and "Alice" in r.message


async def test_retry_double_tap_sends_once(nt):
    import asyncio
    e = nt.env
    e.ctx.fail = HubError("RM offline")
    await e.add(sched())
    await e.tick(FIRE + timedelta(seconds=1))
    tok = _failed(nt)
    e.ctx.fail = None
    n0 = len(e.ctx.sent)
    a, b = await asyncio.gather(nt.retry(ALICE, tok), nt.retry(ALICE, tok))
    assert sorted([a.ok, b.ok]) == [False, True] and len(e.ctx.sent) == n0 + 1


async def test_retry_refuses_changed_or_deleted_schedule(nt):
    from dataclasses import replace
    e = nt.env
    e.ctx.fail = HubError("RM offline")
    await e.add(sched())
    await e.tick(FIRE + timedelta(seconds=1))
    tok = _failed(nt)
    e.ctx.fail = None
    await e.store.mutate(lambda d: d.update({"s7f3a": replace(d["s7f3a"], rev=2)}))
    assert (await nt.retry(ALICE, tok)).error == "stale"
    await e.store.mutate(lambda d: d.pop("s7f3a"))
    assert (await nt.retry(ALICE, tok)).error == "not_found"


async def test_no_retry_for_a_toggle_that_may_have_run(nt):
    import dataclasses
    e = nt.env
    dev = e.ctx.registry.devices["old_ac"]
    dev.features["power_on"] = dataclasses.replace(dev.features["power_on"], idempotent=False)
    e.ctx.fail = HubError("timeout", maybe_delivered=True)
    await e.add(sched(device="old_ac", action=sm.Capture("power_on")))
    await e.tick(FIRE + timedelta(seconds=1))
    (n,) = nt.sent
    assert "may have run anyway" in n.text and buttons(n) == ["pause"]


async def test_undo_to_an_off_state(nt):
    e = nt.env
    await e.ctx.devices.run("bed_ac", "power_off", ALICE)
    await e.add(sched())
    await e.tick(FIRE + timedelta(seconds=1))
    r = await nt.undo(ALICE, decode(nt.sent[0].buttons[0][0][1])[2])
    assert r.ok and e.ctx.ac.last("bed_ac").state.power is False


async def test_failed_undo_can_be_retried(nt):
    e = nt.env
    await e.ctx.devices.run("bed_ac", "power_on", ALICE)
    await e.add(sched())
    await e.tick(FIRE + timedelta(seconds=1))
    tok = decode(nt.sent[0].buttons[0][0][1])[2]
    e.ctx.fail = HubError("RM offline")
    assert not (await nt.undo(ALICE, tok)).ok
    assert tok in nt.tokens


async def test_report_problems_never_raises_and_lists_broken(nt, monkeypatch):
    e = nt.env
    await e.add(sched())
    e.store.broken["s0bad"] = "bad time"

    def boom(s):
        raise RuntimeError("odd")

    await nt.report_problems()
    assert {n.user_id for n in nt.sent} == {1, 2} and "s0bad" in nt.sent[0].text
    monkeypatch.setattr(nt.service, "problem", boom)
    await nt.report_problems()          # logged, not raised
