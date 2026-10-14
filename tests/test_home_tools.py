"""Phase 4.4: the assistant's aircon and schedule tools (fake hub: IR only lands in ctx.sent)."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from iotbot.ac.state import AcState
from iotbot.bot import handlers as hd
from iotbot.llm.home import AlreadyError, HomeTools
from iotbot.llm.tools import ArgError, parse_args, schema_problems
from iotbot.result import Actor
from tests.test_ac_service import make_ctx
from tests.test_bot import ME, make_update
from tests.test_chat import say, tg_context
from tests.test_llm import FakeServer, call, completion

ALICE = Actor(ME, "Alice", "llm")
BOB = Actor(222, "Bob", "button")


@pytest.fixture
def home(tmp_path):
    ctx = make_ctx(tmp_path)
    return SimpleNamespace(ctx=ctx, h=HomeTools(ctx), tools={t.name: t for t in HomeTools(ctx).tools(ALICE)})


async def prep(home, name, args):
    t = home.tools[name]
    return await t.prepare(parse_args(t, json.dumps(args)))


async def send(home, device, **state):
    r = await home.ctx.devices.send_ac_state(device, AcState(**{"power": True, "temp": 24, **state}), BOB)
    assert r.ok


def test_only_managed_aircons_and_clean_schemas(home):
    assert home.h.acs() == ["bed_ac", "office_ac"]                  # old_ac (daikin) is not managed
    for t in home.tools.values():
        assert schema_problems(t.parameters) == [], t.name
    assert home.tools["set_ac"].parameters["properties"]["device"]["enum"] == ["bed_ac", "office_ac"]


async def test_state_says_unknown_then_what_who_when(home):
    out = await home.tools["get_home_state"].run({})
    assert "never set this aircon" in out["aircons"][0]["bot_last_sent"] and "physical remote" in out["note"]
    await send(home, "bed_ac", fan=3)
    row = (await home.tools["get_home_state"].run({}))["aircons"][0]
    assert row["bot_last_sent"] == "ON cool 24C fan 3 vane auto" and row["sent_via_bot_by"] == "Bob"
    assert row["power_as_last_sent"] == "on" and "just now" in row["sent_at"]


async def test_set_ac_line_run_and_undo(home):
    await send(home, "bed_ac")
    p = await prep(home, "set_ac", {"device": "bed_ac", "temp": 23, "vane": "swing"})
    assert p.line == "ON cool 24C fan auto vane auto -> ON cool 23C fan auto vane swing"
    sent = len(home.ctx.sent)
    done = await p.run(ALICE)
    assert done.ok and len(home.ctx.sent) == sent + 1
    assert home.ctx.ac.last("bed_ac").state == AcState(power=True, temp=23, vane="swing")
    u = await done.undo(ALICE)
    assert u.ok and home.ctx.ac.last("bed_ac").state == AcState(power=True, temp=24)


async def test_settings_on_an_off_aircon_turn_it_on(home):
    await send(home, "bed_ac", power=False)
    p = await prep(home, "set_ac", {"device": "bed_ac", "temp": 23})
    assert p.line == "OFF (cool 24C fan auto vane auto) -> ON cool 23C fan auto vane auto"
    for args in ({"power": False}, {"power": False, "temp": 20}):      # off keeps the settings
        with pytest.raises(ArgError, match="already"):
            await prep(home, "set_ac", {"device": "bed_ac", **args})


async def test_temp_change_needs_a_record_and_stays_in_range(home):
    with pytest.raises(ArgError, match="does not know"):
        await prep(home, "set_ac", {"device": "bed_ac", "temp_change": -1})
    await send(home, "bed_ac", temp=16)
    with pytest.raises(ArgError, match="temp must be"):
        await prep(home, "set_ac", {"device": "bed_ac", "temp_change": -1})
    p = await prep(home, "set_ac", {"device": "bed_ac", "temp_change": 2})
    assert p.line.endswith("-> ON cool 18C fan auto vane auto")
    with pytest.raises(ArgError, match="not both"):
        await prep(home, "set_ac", {"device": "bed_ac", "temp_change": 2, "temp": 20})
    with pytest.raises(ArgError, match="say what to change"):
        await prep(home, "set_ac", {"device": "bed_ac"})


async def test_unknown_state_uses_the_room_preset_and_cannot_undo(home):
    p = await prep(home, "set_ac", {"device": "office_ac", "power": True})
    assert p.line.startswith("unknown -> ON ")
    done = await p.run(ALICE)
    assert done.ok and (await done.undo(ALICE)).text == "no earlier state known"


async def test_card_goes_stale_and_undo_refuses_after_a_newer_send(home):
    await send(home, "bed_ac")
    p = await prep(home, "set_ac", {"device": "bed_ac", "temp": 22})
    await send(home, "bed_ac", temp=26)                              # /keyboard meanwhile
    assert p.changed()
    p = await prep(home, "set_ac", {"device": "bed_ac", "temp": 22})
    done = await p.run(ALICE)
    await send(home, "bed_ac", temp=27)
    u = await done.undo(ALICE)
    assert not u.ok and "changed since by Bob" in u.text and home.ctx.ac.last("bed_ac").state.temp == 27


async def test_failed_send_says_why(home):
    from iotbot.devices.hub import HubError

    home.ctx.fail = HubError("no reply")
    p = await prep(home, "set_ac", {"device": "bed_ac", "power": True})
    done = await p.run(ALICE)
    assert not done.ok and "no reply" in done.text


# ---- schedules ---------------------------------------------------------------------------

async def test_timer_in_minutes_then_undo_deletes_it(home):
    p = await prep(home, "add_schedule", {"device": "bed_ac", "action": "off", "in_minutes": 120})
    assert p.name == "new timer" and "bed_ac off" in p.line
    done = await p.run(ALICE)
    assert done.ok and len(home.ctx.schedules.list()) == 1
    assert (await done.undo(ALICE)).ok and home.ctx.schedules.list() == []


async def test_weekly_and_next_occurrence(home):
    p = await prep(home, "add_schedule", {"device": "bed_ac", "action": "on", "at": "23:00", "days": ["sun", "mon"],
                                          "temp": 22, "fan": "3"})
    assert p.name == "new schedule" and "bed_ac on, cool 22C fan 3" in p.line
    assert (await p.run(ALICE)).ok
    s = home.ctx.schedules.list()[0]
    assert s.when.days == ("mon", "sun") and s.action.state.fan == 3
    p = await prep(home, "add_schedule", {"device": "office_ac", "action": "set", "at": "00:01", "temp": 25})
    assert "office_ac set temp 25, once, " in p.line and "00:01" in p.line


@pytest.mark.parametrize("args,why", [
    ({"action": "off"}, "say when"),
    ({"action": "off", "days": ["mon"]}, "needs at and days"),
    ({"action": "off", "at": "23:00", "days": ["mon"], "date": "2030-01-01"}, "needs at and days"),
    ({"action": "off", "in_minutes": 5, "at": "23:00"}, "not both"),
    ({"action": "off", "at": "23:00", "temp": 22}, "takes no settings"),
    ({"action": "set", "at": "23:00"}, "needs at least one"),
    ({"action": "on", "at": "24:00"}, "00:00-23:59"),
    ({"action": "on", "at": "10:00", "date": "2020-01-01"}, "already passed"),
    ({"action": "on", "date": "2030-01-01"}, "needs at"),
    ({"action": "on", "at": "10:00", "date": "2030-02-31"}, "not a real date"),
])
async def test_bad_schedule_requests_go_back_to_the_model(home, args, why):
    with pytest.raises(ArgError, match=why):
        await prep(home, "add_schedule", {"device": "bed_ac", **args})


async def test_edit_shows_before_and_after_and_undo_restores(home):
    p = await prep(home, "add_schedule", {"device": "bed_ac", "action": "off", "at": "07:00", "days": ["mon"]})
    await p.run(ALICE)
    sid = home.ctx.schedules.list()[0].id
    await (await prep(home, "edit_schedule", {"schedule_id": sid, "label": "Wake"})).run(ALICE)
    p = await prep(home, "edit_schedule", {"schedule_id": sid, "at": "07:30"})
    assert p.name == "schedule Wake" and p.line.startswith("Wake: bed_ac off, 07:00") and "-> Wake: bed_ac off, 07:30" in p.line
    done = await p.run(ALICE)
    assert done.ok and home.ctx.schedules.get(sid).when.time == "07:30"
    assert (await done.undo(ALICE)).ok and home.ctx.schedules.get(sid).when.time == "07:00"
    with pytest.raises(ArgError, match="no schedule nope"):
        await prep(home, "edit_schedule", {"schedule_id": "nope", "at": "07:30"})
    with pytest.raises(ArgError, match="no date"):
        await prep(home, "edit_schedule", {"schedule_id": sid, "date": "2030-01-01"})
    with pytest.raises(ArgError, match="nothing would change: Wake already does that"):
        await prep(home, "edit_schedule", {"schedule_id": sid, "at": "07:00"})
    with pytest.raises(ArgError, match="Wake does 'off'; say action on or set"):
        await prep(home, "edit_schedule", {"schedule_id": sid, "temp": 23})
    p = await prep(home, "edit_schedule", {"schedule_id": sid, "action": "set", "temp": 23})
    assert "set temp 23" in p.line


async def test_edit_goes_stale_when_the_schedule_changes(home):
    p = await prep(home, "add_schedule", {"device": "bed_ac", "action": "off", "at": "07:00", "days": ["mon"]})
    await p.run(ALICE)
    sid = home.ctx.schedules.list()[0].id
    p = await prep(home, "edit_schedule", {"schedule_id": sid, "at": "08:00"})
    other = await prep(home, "edit_schedule", {"schedule_id": sid, "at": "09:00"})
    assert (await other.run(ALICE)).ok
    assert p.changed()


async def test_delete_pause_resume_with_undo(home):
    p = await prep(home, "add_schedule", {"device": "bed_ac", "action": "off", "at": "07:00", "days": ["mon"]})
    await p.run(ALICE)
    sid = home.ctx.schedules.list()[0].id
    p = await prep(home, "pause_schedule", {"schedule_id": sid, "until": "2099-01-01"})
    assert p.line.startswith("pause until Thu 01 Jan:")
    done = await p.run(ALICE)
    assert done.ok and home.ctx.schedules.get(sid).paused_until is not None
    listed = (await home.tools["list_schedules"].run({}))["schedules"][0]
    assert listed["id"] == sid and listed["status"].startswith("paused until")
    p = await prep(home, "resume_schedule", {"schedule_id": sid})
    assert (await p.run(ALICE)).ok and home.ctx.schedules.get(sid).paused_until is None
    with pytest.raises(ArgError, match="not paused"):
        await prep(home, "resume_schedule", {"schedule_id": sid})
    p = await prep(home, "delete_schedule", {"schedule_id": sid})
    done = await p.run(ALICE)
    assert done.ok and home.ctx.schedules.get(sid) is None
    assert (await done.undo(ALICE)).ok and home.ctx.schedules.get(sid) is not None
    with pytest.raises(ArgError, match="future"):
        await prep(home, "pause_schedule", {"schedule_id": sid, "until": "2020-01-01"})


async def test_pause_refuses_an_already_paused_schedule(home):
    p = await prep(home, "add_schedule", {"device": "bed_ac", "action": "off", "at": "07:00", "days": ["mon"]})
    await p.run(ALICE)
    sid = home.ctx.schedules.list()[0].id
    assert (await (await prep(home, "pause_schedule", {"schedule_id": sid})).run(ALICE)).ok
    with pytest.raises(AlreadyError, match="already paused"):
        await prep(home, "pause_schedule", {"schedule_id": sid})
    # Paused for good -> paused until a date is a real change; the same date again is not
    p = await prep(home, "pause_schedule", {"schedule_id": sid, "until": "2099-01-01"})
    assert (await p.run(ALICE)).ok
    with pytest.raises(AlreadyError, match="already paused"):
        await prep(home, "pause_schedule", {"schedule_id": sid, "until": "2099-01-01"})


async def test_card_names_hide_ids_and_two_new_schedules_fit_one_card(home):
    from iotbot.llm import loop as lp
    calls = [lp.Action(home.tools["add_schedule"], {"device": "bed_ac", "action": "on", "at": "07:00", "days": ["mon"]}),
             lp.Action(home.tools["add_schedule"], {"device": "bed_ac", "action": "off", "at": "09:00", "days": ["mon"]})]
    assert await lp._prepare(calls) == {}
    await calls[0].prepared.run(ALICE)
    sid = home.ctx.schedules.list()[0].id
    twice = [lp.Action(home.tools["pause_schedule"], {"schedule_id": sid}),
             lp.Action(home.tools["delete_schedule"], {"schedule_id": sid})]
    refused = await lp._prepare(twice)
    assert len(refused) == 1 and "already changes that" in next(iter(refused.values()))
    assert twice[0].prepared.name == "schedule" and sid not in twice[0].prepared.name


# ---- end to end --------------------------------------------------------------------------

async def test_chat_to_card_to_ir_and_undo(tmp_path):
    ctx = make_ctx(tmp_path)
    await ctx.devices.send_ac_state("bed_ac", AcState(power=True, temp=24), BOB)
    ctx.assistant.llm = FakeServer([
        completion(calls=[call("get_home_state", {})]),
        completion("Done, cooler now!", [call("set_ac", {"device": "bed_ac", "temp_change": -1}, "c2")]),
    ]).client()
    context = tg_context(ctx)
    u = say("a bit cooler in the bedroom")
    u.effective_message.reply_text = AsyncMock(return_value=SimpleNamespace(chat_id=ME, message_id=9))
    await hd.on_text(u, context)
    text = u.effective_message.reply_text.call_args.args[0]
    assert "<b>bed_ac</b>: ON cool 24C fan auto vane auto -&gt; ON cool 23C fan auto vane auto" in text
    sent = len(ctx.sent)
    card = next(iter(ctx.assistant.cards.cards.values()))
    t = make_update(ME, data=f"lc:do:{card.id}")
    await hd.on_button(t, context)
    assert len(ctx.sent) == sent + 1 and ctx.ac.last("bed_ac").state.temp == 23
    assert ctx.ac.last("bed_ac").actor == "Alice"
    t = make_update(ME, data=f"lc:undo:{card.id}")
    await hd.on_button(t, context)
    assert len(ctx.sent) == sent + 2 and ctx.ac.last("bed_ac").state.temp == 24


# ---- review follow-ups -------------------------------------------------------------------

def freeze(home, when):
    home.ctx.schedules.now = lambda: when


async def test_a_card_tapped_after_its_timer_time_saves_nothing(home):
    from datetime import timedelta

    start = home.ctx.schedules.now()
    p = await prep(home, "add_schedule", {"device": "bed_ac", "action": "off", "in_minutes": 2})
    freeze(home, start + timedelta(minutes=4))
    done = await p.run(ALICE)
    assert not done.ok and "already passed" in done.text and home.ctx.schedules.list() == []


async def test_in_minutes_rounds_up(home):
    from datetime import datetime

    freeze(home, datetime(2030, 1, 7, 12, 0, 45, tzinfo=home.h.tz))
    p = await prep(home, "add_schedule", {"device": "bed_ac", "action": "off", "in_minutes": 1})
    assert "12:02" in p.line


async def test_timer_edit_keeps_its_day(home):
    p = await prep(home, "add_schedule", {"device": "bed_ac", "action": "off", "at": "08:00", "date": "2099-01-02"})
    await p.run(ALICE)
    sid = home.ctx.schedules.list()[0].id
    p = await prep(home, "edit_schedule", {"schedule_id": sid, "at": "09:00"})
    await p.run(ALICE)
    assert home.ctx.schedules.get(sid).when.at.strftime("%Y-%m-%d %H:%M") == "2099-01-02 09:00"
    p = await prep(home, "edit_schedule", {"schedule_id": sid, "date": "2099-01-03"})
    await p.run(ALICE)
    assert home.ctx.schedules.get(sid).when.at.strftime("%Y-%m-%d %H:%M") == "2099-01-03 09:00"
    with pytest.raises(ArgError, match="cannot pause until a date"):
        await prep(home, "pause_schedule", {"schedule_id": sid, "until": "2099-01-01"})


async def test_editing_a_set_schedule_keeps_the_other_settings(home):
    p = await prep(home, "add_schedule", {"device": "bed_ac", "action": "set", "at": "14:00", "days": ["mon"],
                                          "temp": 24, "fan": "3"})
    await p.run(ALICE)
    sid = home.ctx.schedules.list()[0].id
    p = await prep(home, "edit_schedule", {"schedule_id": sid, "temp": 25})
    await p.run(ALICE)
    assert dict(home.ctx.schedules.get(sid).action.changes) == {"temp": 25, "fan": 3}


async def test_schedules_of_other_devices_are_out_of_reach(home):
    from iotbot.result import Actor as A

    svc = home.ctx.schedules
    r = svc.plan(A(ME, "Alice", "button"), {"device": "old_ac", "action": {"kind": "capture", "key": "power_on"},
                                            "when": {"kind": "weekly", "time": "07:00", "days": ["mon"]}})
    assert r.ok, r.message
    assert (await svc.apply(A(ME, "Alice", "button"), r.data["plan_id"])).ok
    sid = svc.list()[0].id
    assert (await home.tools["list_schedules"].run({}))["schedules"] == "none"
    with pytest.raises(ArgError, match="no schedule"):
        await prep(home, "delete_schedule", {"schedule_id": sid})


async def test_expired_pause_reads_as_on(home):
    from dataclasses import replace
    from datetime import timedelta

    p = await prep(home, "add_schedule", {"device": "bed_ac", "action": "off", "at": "07:00", "days": ["mon"]})
    await p.run(ALICE)
    s = home.ctx.schedules.list()[0]
    past = home.ctx.schedules.now() - timedelta(days=1)
    await home.ctx.schedules.store.mutate(lambda d: d.__setitem__(s.id, replace(d[s.id], paused_until=past)))
    assert (await home.tools["list_schedules"].run({}))["schedules"][0]["status"] == "active"
    with pytest.raises(ArgError, match="not paused"):
        await prep(home, "resume_schedule", {"schedule_id": s.id})


async def test_a_send_between_check_and_lock_is_never_overwritten(home):
    await send(home, "bed_ac")
    p = await prep(home, "set_ac", {"device": "bed_ac", "temp": 22})
    # A schedule fires after the card's begin() check, before the send takes the AC lock
    await send(home, "bed_ac", temp=27)
    done = await p.run(ALICE)
    assert not done.ok and "changed since by Bob" in done.text and home.ctx.ac.last("bed_ac").state.temp == 27


async def test_retry_after_a_failed_send_still_works(home):
    from iotbot.devices.hub import HubError
    from iotbot.llm.cards import Cards

    await send(home, "bed_ac")
    cards = Cards()
    card, _ = cards.new(ME, [await prep(home, "set_ac", {"device": "bed_ac", "temp": 22})])
    home.ctx.fail = HubError("no reply")
    cards.begin(card, "do")
    await cards.run(card, "do", ALICE)
    assert not card.results[0].ok
    home.ctx.fail = None
    cards.begin(card, "retry")
    await cards.run(card, "retry", ALICE)
    assert card.results[0].ok and home.ctx.ac.last("bed_ac").state.temp == 22


async def test_keep_both_only_for_the_clashes_on_the_card(home):
    p = await prep(home, "add_schedule", {"device": "bed_ac", "action": "on", "at": "23:00", "days": ["mon"]})
    await p.run(ALICE)
    clash = await prep(home, "add_schedule", {"device": "bed_ac", "action": "off", "at": "23:00", "days": ["mon"]})
    assert "Clashes with" in clash.line
    other = await prep(home, "add_schedule", {"device": "bed_ac", "action": "on", "at": "23:00", "days": ["mon"],
                                              "temp": 20})
    assert (await other.run(ALICE)).ok                           # a new clash the first card never showed
    done = await clash.run(ALICE)
    assert not done.ok and "clashes changed" in done.text and len(home.ctx.schedules.list()) == 2


async def test_schedule_card_notes_a_public_holiday_run(home):
    svc = home.ctx.schedules
    day = (svc.now() + timedelta(days=3)).date()
    home.ctx.holidays.days[day] = "Test Day"
    p = await prep(home, "add_schedule", {"device": "bed_ac", "action": "off", "at": "08:00", "date": day.isoformat()})
    assert p.line.endswith(f"Runs on a public holiday: {day:%a} {day.day} {day:%b} (Test Day)")
    p = await prep(home, "add_schedule", {"device": "bed_ac", "action": "off", "at": "08:00",
                                          "date": (day + timedelta(days=1)).isoformat()})
    assert "public holiday" not in p.line
