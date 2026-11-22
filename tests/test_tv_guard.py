"""Phase 10.14: the bedroom TV only has the power toggle (RC6 12). power_on / power_off both send it,
behind DeviceService's guard: a repeat of what the bot itself last sent (last GUARD_HOURS) is refused,
with Send anyway everywhere a person is looking. Fake hub: IR only lands in ctx.sent."""

import json

import pytest

from iotbot.bot import handlers as hd
from iotbot.devices.hub import HubError
from iotbot.llm.home import HomeTools
from iotbot.llm.tools import AlreadyError, message_text, parse_args
from iotbot.result import Actor
from iotbot.schedule import model as sm
from iotbot.schedule.runner import Scheduler
from iotbot.schedule.store import ScheduleStore
from tests.test_ac_service import make_ctx
from tests.test_bot import ME, make_context, make_update

ALICE = Actor(ME, "Alice", "llm")


@pytest.fixture
def ctx(tmp_path):
    return make_ctx(tmp_path, tv="philips-rc6")


async def prep(ctx, args, said):
    message_text.set(said)
    t = {t.name: t for t in HomeTools(ctx).tools(ALICE)}["set_device"]
    return await t.prepare(parse_args(t, json.dumps(args)))


OFF, ON = {"device": "bed_tv", "power": False}, {"device": "bed_tv", "power": True}


async def test_chat_already_off_says_send_anyway_flips_it(ctx):
    await (await prep(ctx, OFF, "tv off")).run(ALICE)
    with pytest.raises(AlreadyError) as e:
        await prep(ctx, OFF, "tv off")
    assert e.value.short == "bed_tv: already OFF."
    assert "power key is a toggle: if it really is OFF, Send anyway turns it ON" in str(e.value)
    assert (await e.value.force.run(ALICE)).ok and len(ctx.sent) == 2


async def test_chat_maybe_sent_counts_as_done_and_has_no_undo(ctx):
    ctx.fail = HubError("timeout", maybe_delivered=True)
    done = await (await prep(ctx, OFF, "tv off")).run(ALICE)
    assert not done.ok and "may still have worked" in done.text
    ctx.fail = None
    with pytest.raises(AlreadyError) as e:
        await prep(ctx, OFF, "tv off")
    assert e.value.short == "bed_tv: may already be OFF." and "may or may not have gone through" in str(e.value)
    on = await prep(ctx, ON, "tv on")
    assert on.line == "maybe OFF -> ON"
    done = await on.run(ALICE)
    assert done.ok and done.undo is None                    # the state before was a guess


async def test_chat_card_retry_after_a_maybe_send_is_refused_by_the_guard(ctx):
    item = await prep(ctx, OFF, "tv off")
    ctx.fail = HubError("timeout", maybe_delivered=True)
    assert not (await item.run(ALICE)).ok
    ctx.fail = None
    item.seen = item.version()                              # what Cards does after Do
    again = await item.run(ALICE)                           # [Retry failed]
    assert not again.ok and "may already be OFF" in again.text and ctx.sent == []


async def test_send_anyway_retry_does_not_force_twice_after_a_maybe_send(ctx):
    await (await prep(ctx, OFF, "tv off")).run(ALICE)
    with pytest.raises(AlreadyError) as e:
        await prep(ctx, OFF, "tv off")
    item = e.value.force
    ctx.fail = HubError("timeout", maybe_delivered=True)
    assert not (await item.run(ALICE)).ok
    ctx.fail = None
    item.seen = item.version()
    again = await item.run(ALICE)
    assert not again.ok and again.text.startswith("bed_tv may already be OFF") and len(ctx.sent) == 1


async def test_send_anyway_after_a_definite_failure_still_forces(ctx):
    await (await prep(ctx, OFF, "tv off")).run(ALICE)
    with pytest.raises(AlreadyError) as e:
        await prep(ctx, OFF, "tv off")
    item = e.value.force
    ctx.fail = HubError("RM offline", maybe_delivered=False)
    assert not (await item.run(ALICE)).ok
    ctx.fail = None
    item.seen = item.version()
    assert (await item.run(ALICE)).ok and len(ctx.sent) == 2


async def test_home_state_says_when_a_send_may_have_gone_through(ctx):
    ctx.fail = HubError("timeout", maybe_delivered=True)
    await (await prep(ctx, OFF, "tv off")).run(ALICE)
    t = {t.name: t for t in HomeTools(ctx).tools(ALICE)}["get_home_state"]
    row = (await t.run({}))["tvs"][0]
    assert row["bot_last_sent"] == "off" and row["send_result"] == "failed, may have been sent"


def scheduler(ctx, tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Singapore")
    store = ScheduleStore(tmp_path / "state" / "schedules.json")
    store.load()
    return Scheduler(store, ctx.devices, tz, clock=lambda: datetime(2026, 11, 22, 23, tzinfo=tz).timestamp(),
                     synced=lambda: None), datetime(2026, 11, 22, 23, 0, tzinfo=tz)


async def test_timer_off_is_skipped_when_the_bot_already_turned_it_off(ctx, tmp_path):
    from tests.test_schedule_runner import sched

    sch, fire = scheduler(ctx, tmp_path)
    o = await sch.execute(sched(device="bed_tv", action=sm.Off()), fire, "k", actor=ALICE, retries=False)
    assert o.result == "ok" and len(ctx.sent) == 1          # no record yet: sent
    o = await sch.execute(sched(device="bed_tv", action=sm.Off()), fire, "k2", actor=ALICE, retries=False)
    assert o.result == "skipped" and "already OFF" in o.reason and "toggle" in o.reason and len(ctx.sent) == 1


async def test_timer_on_runs_after_an_off(ctx, tmp_path):
    from tests.test_schedule_runner import sched

    sch, fire = scheduler(ctx, tmp_path)
    await sch.execute(sched(device="bed_tv", action=sm.Off()), fire, "k", actor=ALICE, retries=False)
    o = await sch.execute(sched(device="bed_tv", action=sm.Capture("power_on")), fire, "k2", actor=ALICE,
                          retries=False)
    assert o.result == "ok" and len(ctx.sent) == 2


async def test_a_guarded_timer_is_not_retried_after_a_maybe_send(ctx, tmp_path):
    from tests.test_schedule_runner import sched

    sch, fire = scheduler(ctx, tmp_path)
    sch._sleep = lambda s: _noop()
    ctx.fail = HubError("timeout", maybe_delivered=True)
    o = await sch.execute(sched(device="bed_tv", action=sm.Off()), fire, "k", actor=ALICE, retries=True)
    assert o.result == "failed" and o.attempts == 1


async def _noop():
    return None


async def test_keyboard_button_refusal_offers_send_anyway(ctx, monkeypatch):
    sent = []

    async def fake_dm(query, text, markup=None):
        sent.append((text, markup))

    monkeypatch.setattr(hd, "dm", fake_dm)
    u = make_update(ME, data="kb:f:bed_tv:power_off")
    await hd.on_button(u, make_context(ctx))
    assert len(ctx.sent) == 1
    u = make_update(ME, data="kb:f:bed_tv:power_off")
    await hd.on_button(u, make_context(ctx))
    assert len(ctx.sent) == 1 and "is already OFF" in sent[0][0]
    btn = sent[0][1].inline_keyboard[0][0]
    assert btn.text == "Send anyway"
    u = make_update(ME, data=btn.callback_data)
    await hd.on_button(u, make_context(ctx))
    assert len(ctx.sent) == 2
    assert u.callback_query.edit_message_text.call_args.args[0].startswith("Sent bed_tv power off")


async def test_an_old_send_anyway_is_refused_after_anything_newer(ctx, monkeypatch):
    sent = []

    async def fake_dm(query, text, markup=None):
        sent.append((text, markup))

    monkeypatch.setattr(hd, "dm", fake_dm)
    for _ in range(2):
        await hd.on_button(make_update(ME, data="kb:f:bed_tv:power_off"), make_context(ctx))
    old = sent[0][1].inline_keyboard[0][0].callback_data
    await hd.on_button(make_update(ME, data="kb:f:bed_tv:power_on"), make_context(ctx))
    assert len(ctx.sent) == 2
    u = make_update(ME, data=old)
    await hd.on_button(u, make_context(ctx))
    assert len(ctx.sent) == 2
    assert "Not sent" in u.callback_query.edit_message_text.call_args.args[0]
    # Send anyway exists only for a guarded toggle
    u = make_update(ME, data="kb:F:bed_ac:power_off:n")
    await hd.on_button(u, make_context(ctx))
    assert u.callback_query.answer.call_args.args[0] == "This button is no longer active." and len(ctx.sent) == 2


async def test_slash_d_refusal_offers_send_anyway(ctx):
    from tests.test_bot import replied

    await hd.cmd_device(make_update(ME), make_context(ctx, ["bed_tv", "off"]))
    u = make_update(ME)
    await hd.cmd_device(u, make_context(ctx, ["bed_tv", "off"]))
    assert len(ctx.sent) == 1 and "is already OFF" in replied(u)
    markup = u.effective_message.reply_text.call_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data.startswith("kb:F:bed_tv:power_off:")


def test_send_anyway_callback_fits_with_the_longest_id():
    from types import SimpleNamespace

    from iotbot.services.devices import LastAction

    ctx = SimpleNamespace(devices=SimpleNamespace(last={"x" * 32: LastAction(10 ** 6, "power_off", 4102444800.0,
                                                                            "a", True, None)}))
    data = hd._anyway_markup(ctx, "x" * 32, "power_off").inline_keyboard[0][0].callback_data
    assert len(data.encode()) <= 64 and data.startswith("kb:F:" + "x" * 32 + ":power_off:")


async def test_overlapping_presses_wait_and_the_second_sees_the_first_outcome(ctx):
    import asyncio

    gate = asyncio.Event()
    orig = ctx.hub.send_ir
    calls = []

    async def slow(mac, packets, gap_s=0.0, idempotent=False):
        calls.append(1)
        await gate.wait()
        if getattr(ctx, "fail", None):
            raise ctx.fail
        await orig(mac, packets, gap_s, idempotent)

    ctx.hub.send_ir = slow
    a = Actor(ME, "Alice", "button")
    first = asyncio.create_task(ctx.devices.run("bed_tv", "power_off", a))
    await asyncio.sleep(0)
    second = asyncio.create_task(ctx.devices.run("bed_tv", "power_off", a))
    await asyncio.sleep(0)
    assert len(calls) == 1                                   # the second waits for the first
    gate.set()
    r1, r2 = await first, await second
    assert r1.ok and r2.error == "already_state" and r2.data["sure"] and len(ctx.sent) == 1


async def test_overlapping_opposite_presses_that_both_fail_leave_the_old_record(ctx):
    # review 2026-11-22: a definite failure used to restore the other press's in-flight record
    import asyncio

    a = Actor(ME, "Alice", "button")
    assert (await ctx.devices.run("bed_tv", "power_off", a)).ok
    ctx.fail = HubError("RM offline", maybe_delivered=False)
    r_on, r_off = await asyncio.gather(ctx.devices.run("bed_tv", "power_on", a),
                                       ctx.devices.run("bed_tv", "power_off", a))
    assert not r_on.ok and r_on.error == "send_failed"
    assert r_off.error == "already_state"                    # checked after the on failed: still OFF
    lp = ctx.devices.known_power("bed_tv")
    assert (lp.feature, lp.ok) == ("power_off", True)


async def test_a_repeating_tv_schedule_warns_and_a_timer_against_the_record_warns(ctx):
    from tests.test_schedule_runner import sched

    rep = sched(device="bed_tv", action=sm.Off())
    assert not rep.is_timer
    _, w = ctx.schedules.conflicts(rep)
    assert any("power key is a toggle" in x and "a run turns it on" in x for x in w)
    assert (await ctx.devices.run("bed_tv", "power_off", ALICE)).ok
    message_text.set("tv off in 30 min")
    tl = {t.name: t for t in HomeTools(ctx).tools(ALICE)}["add_schedule"]
    p = await tl.prepare(parse_args(tl, json.dumps({"device": "bed_tv", "action": "off", "in_minutes": 30})))
    assert "already OFF" in p.line and "this timer will be skipped" in p.line


async def test_a_late_refusal_still_sends_the_send_anyway_message(ctx, monkeypatch):
    from unittest.mock import AsyncMock

    from telegram.error import BadRequest

    sent = []

    async def fake_dm(query, text, markup=None):
        sent.append((text, markup))

    monkeypatch.setattr(hd, "dm", fake_dm)
    await hd.on_button(make_update(ME, data="kb:f:bed_tv:power_off"), make_context(ctx))
    u = make_update(ME, data="kb:f:bed_tv:power_off")
    u.callback_query.answer = AsyncMock(side_effect=BadRequest("Query is too old"))
    await hd.on_button(u, make_context(ctx))
    assert len(sent) == 1 and sent[0][1].inline_keyboard[0][0].text == "Send anyway"
