"""Phase 10: the TV in chat (HomeTools / MemoryTools). Fake hub: IR only lands in ctx.sent."""

import json

import pytest

from iotbot.ir import packet, rc6
from iotbot.ir.codesets import CODESETS, Button, Codeset
from iotbot.llm.home import HomeTools
from iotbot.llm.tools import AlreadyError, ArgError, message_text, parse_args
from iotbot.result import Actor
from tests.test_ac_service import make_ctx
from tests.test_bot import ME

ALICE = Actor(ME, "Alice", "llm")
OFF_CMD = CODESETS["philips-rc6"].buttons["power_off"].command
ON_CMD = CODESETS["philips-rc6"].buttons["power_on"].command


@pytest.fixture
def ctx(tmp_path):
    return make_ctx(tmp_path, tv="philips-rc6")


def tools(ctx, actor=ALICE):
    return {t.name: t for t in HomeTools(ctx).tools(actor)}


async def prep(ctx, name, args, said):
    message_text.set(said)
    t = tools(ctx)[name]
    return await t.prepare(parse_args(t, json.dumps(args)))


def sent_cmds(ctx):
    return [rc6.decode(packet.to_pulses(pkts[0]))[0][1] for _, pkts, _ in ctx.sent]


def test_tv_is_offered_to_chat_with_power_only(ctx):
    h = HomeTools(ctx)
    assert h.tvs() == ["bed_tv"] and h.chat_devices() == ["bed_ac", "office_ac", "bed_tv"]
    t = tools(ctx)["set_device"]
    assert "bed_tv" in t.parameters["properties"]["device"]["enum"] and "TV: power only" in t.description


def test_a_toggle_only_tv_stays_out_of_chat(tmp_path, monkeypatch):
    monkeypatch.setitem(CODESETS, "test-toggle", Codeset("test-toggle", "rc6", 0, {"power": Button(12, False)}))
    c = make_ctx(tmp_path, tv="test-toggle")
    assert HomeTools(c).tvs() == [] and "TV" not in tools(c)["set_device"].description


async def test_tv_off_line_run_and_no_undo_when_the_state_was_unknown(ctx):
    p = await prep(ctx, "set_device", {"device": "bed_tv", "power": False}, "turn off the tv")
    assert (p.name, p.line) == ("bed_tv", "unknown -> OFF")
    done = await p.run(ALICE)
    assert done.ok and done.undo is None and sent_cmds(ctx) == [OFF_CMD]


async def test_tv_is_only_pressed_when_the_message_names_it(ctx):
    for said in ("off bedroom", "off bedroom pls", "bedroom off now"):
        with pytest.raises(ArgError, match="does not name the TV"):
            await prep(ctx, "set_device", {"device": "bed_tv", "power": False}, said)
    for said in ("switch off the television", "telly off", "turn everything off", "all off"):
        assert (await prep(ctx, "set_device", {"device": "bed_tv", "power": False}, said)).name == "bed_tv"


async def test_a_note_can_tie_a_room_word_to_the_tv(ctx):
    await ctx.memory.add_fact(ALICE, "When I say bedroom I mean the TV", False)
    assert (await prep(ctx, "set_device", {"device": "bed_tv", "power": False}, "off bedroom")).name == "bed_tv"


async def test_tv_takes_no_settings(ctx):
    with pytest.raises(ArgError, match="only be switched on or off"):
        await prep(ctx, "set_device", {"device": "bed_tv", "temp": 24}, "tv 24")
    with pytest.raises(ArgError, match="say power"):
        await prep(ctx, "set_device", {"device": "bed_tv"}, "the tv")


async def test_already_off_offers_send_anyway_and_undo_needs_a_known_state(ctx):
    await (await prep(ctx, "set_device", {"device": "bed_tv", "power": False}, "tv off")).run(ALICE)
    with pytest.raises(AlreadyError) as e:
        await prep(ctx, "set_device", {"device": "bed_tv", "power": False}, "tv off")
    assert e.value.short == "bed_tv: already OFF." and e.value.force.do_label == "Send anyway"
    assert (await e.value.force.run(ALICE)).ok and sent_cmds(ctx) == [OFF_CMD, OFF_CMD]
    on = await prep(ctx, "set_device", {"device": "bed_tv", "power": True}, "tv on")
    assert on.line == "OFF -> ON"
    done = await on.run(ALICE)
    assert done.undo is not None and (await done.undo(ALICE)).ok
    assert sent_cmds(ctx) == [OFF_CMD, OFF_CMD, ON_CMD, OFF_CMD]


async def test_aircon_already_set_also_offers_send_anyway(ctx):
    await (await prep(ctx, "set_device", {"device": "bed_ac", "power": False}, "off bedroom")).run(ALICE)
    with pytest.raises(AlreadyError) as e:
        await prep(ctx, "set_device", {"device": "bed_ac", "power": False}, "off bedroom")
    assert e.value.short == "bed_ac: already OFF." and e.value.force is not None


async def test_home_state_lists_the_tv_as_last_sent(ctx):
    state = await tools(ctx)["get_home_state"].run({})
    assert state["tvs"][0]["bot_last_sent"].startswith("nothing yet")
    await (await prep(ctx, "set_device", {"device": "bed_tv", "power": True}, "tv on")).run(ALICE)
    row = (await tools(ctx)["get_home_state"].run({}))["tvs"][0]
    assert row["bot_last_sent"] == "on" and row["sent_via_bot_by"] == "Alice" and "send_result" not in row


async def test_tv_timers_on_off_and_only_when_named(ctx):
    off = await prep(ctx, "add_schedule", {"device": "bed_tv", "action": "off", "at": "23:00"}, "tv off at 11pm")
    assert "bed_tv off" in off.line
    on = await prep(ctx, "add_schedule", {"device": "bed_tv", "action": "on", "in_minutes": 30}, "tv on in 30 min")
    assert "bed_tv power on" in on.line
    with pytest.raises(ArgError, match="does not name the TV"):
        await prep(ctx, "add_schedule", {"device": "bed_tv", "action": "off", "at": "23:00"}, "off bedroom at 11pm")
    with pytest.raises(ArgError, match="no settings|only be switched"):
        await prep(ctx, "add_schedule", {"device": "bed_tv", "action": "set", "at": "23:00", "temp": 24}, "tv")


# ---- "Which one?" in code -------------------------------------------------------------------------

from iotbot.llm import assistant as asst  # noqa: E402

DEVS = [("bedroom-aircon", "bedroom", "ac"), ("office-aircon", "office", "ac"), ("bedroom-tv", "bedroom", "tv")]


@pytest.mark.parametrize("text,word", [("off bedroom", "bedroom"), ("off bedroom now", "bedroom"),
                                       ("off bedroom pls", "bedroom"), ("bedroom on at 1130", "bedroom")])
def test_a_room_with_two_devices_asks_with_taps(text, word):
    ask = asst.which_one(text, DEVS, [])
    assert ask.word == word and [o[0] for o in ask.options] == ["bedroom aircon", "bedroom TV", "both"]
    assert ask.options[0][1] == f"{text} (I mean the bedroom aircon)"
    assert ask.text.endswith(": which one?") and ("(now)" in ask.text) == ("now" in text)


@pytest.mark.parametrize("text", ["off office", "bedroom 24", "bedroom fan quiet", "off bedroom aircon",
                                  "off the tv", "turn everything off", "is bedroom on?", "remember bedroom is big",
                                  "off bedroom tv and aircon"])
def test_no_question_when_it_can_only_mean_one_thing(text):
    assert asst.which_one(text, DEVS, []) is None


def test_a_note_about_the_room_or_both_lets_the_model_decide():
    assert asst.which_one("off bedroom", DEVS, ["When I say just 'bedroom', I mean the bedroom aircon."]) is None
    assert asst.which_one("both off", DEVS, ["When I say both I mean the two aircons"]) is None


def test_both_with_three_devices_asks_which_ones():
    ask = asst.which_one("both off now", DEVS, [])
    assert [o[0] for o in ask.options] == ["both aircons", "bedroom aircon and TV"] and ask.word == "both"
    assert asst.which_one("both off", DEVS[:2], []) is None             # two aircons only: no question


def test_my_aircon_question_has_taps_too():
    ask = asst.aircon_options("turn off my aircon", [("bedroom-aircon", "bedroom"), ("office-aircon", "office")])
    assert [o[0] for o in ask.options] == ["bedroom", "office", "both"] and ask.word == "my aircon"
    assert ask.options[1][2] == "When I say 'my aircon', I mean the office aircon."


def test_tv_rules_only_in_a_home_with_a_tv(tmp_path, ctx):
    assert "TVs: switch one only" in ctx.assistant.system_prompt()
    assert "bed_tv: TV in room 'bedroom' (power on/off only)" in ctx.assistant.system_prompt()
    (tmp_path / "plain").mkdir()
    plain = make_ctx(tmp_path / "plain")
    assert "TVs:" not in plain.assistant.system_prompt()


async def test_tap_an_answer_sends_the_order_with_it_and_offers_a_note_once(ctx):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from iotbot.bot import handlers as hd
    from tests.test_bot import make_update
    from tests.test_chat import say, tg_context
    from tests.test_llm import FakeServer, call, completion

    srv = FakeServer([completion(calls=[call("set_device", {"device": "bed_ac", "power": False})])])
    ctx.assistant.llm = srv.client()
    context = tg_context(ctx)
    u = say("off bedroom")
    await hd.on_text(u, context)
    (text,), kw = u.effective_message.reply_text.call_args
    assert text == "Turn off in the bedroom: which one?"
    assert [r[0].text for r in kw["reply_markup"].inline_keyboard] == ["bedroom aircon", "bedroom TV", "both"]
    assert srv.requests == []                                       # asked in code: no LLM call

    first = kw["reply_markup"].inline_keyboard[0][0].callback_data
    assert first.startswith("wh:o:") and first.endswith(":0")
    t = make_update(ME, data=first)
    t.callback_query.message = SimpleNamespace(chat_id=ME, message_id=9)
    t.effective_message.reply_text = AsyncMock(return_value=SimpleNamespace(chat_id=ME, message_id=10))
    await hd.on_button(t, context)
    shown, kw = t.callback_query.edit_message_text.call_args
    assert shown[0] == "Turn off in the bedroom: which one?\n-> bedroom aircon"
    assert kw["reply_markup"].inline_keyboard[0][0].text.startswith("Remember: 'bedroom' means bedroom aircon")
    assert srv.requests[0]["messages"][-1]["content"].endswith("off bedroom (I mean the bedroom aircon)")
    card_text = t.effective_message.reply_text.call_args[0][0]
    assert "bed_ac" in card_text and "Do this?" in card_text

    r = make_update(ME, data="wh:r")
    r.callback_query.message = SimpleNamespace(chat_id=ME, message_id=9)
    await hd.on_button(r, context)
    assert [f["text"] for f in ctx.memory.facts(ME)] == ["When I say just 'bedroom', I mean the bedroom aircon."]
    # The note now lets the model decide: no question the next time
    u2 = say("off bedroom")
    ctx.assistant.llm = FakeServer([completion("ok")]).client()
    await hd.on_text(u2, context)
    assert "which one" not in u2.effective_message.reply_text.call_args[0][0]



# ---- review fixes (2026-11-22) ----------------------------------------------------------------------

def test_stats_reads_the_ir_field():
    from iotbot.bot import stats
    assert "ir" in stats.EVENT_KEYS


def test_two_aircons_in_one_room_are_left_to_the_model():
    devs = [("living-ac-left", "living", "ac"), ("living-ac-right", "living", "ac")]
    assert asst.which_one("off living", devs, []) is None


@pytest.mark.parametrize("text", ["off bedroom at 10:30", "off bedroom in 30 min", "bedroom off at 1am"])
def test_timer_orders_for_a_shared_room_still_ask(text):
    assert asst.which_one(text, DEVS, []).word == "bedroom"


def test_a_typed_answer_is_the_order_with_it():
    ask = asst.which_one("off bedroom now", DEVS, [])
    assert asst._typed_answer("both", ask) == "off bedroom now (I mean the bedroom aircon and the bedroom TV)"
    assert asst._typed_answer("Bedroom TV", ask) == "off bedroom now (I mean the bedroom TV)"
    assert asst._typed_answer("off the office", ask) is None


@pytest.mark.parametrize("said,ok", [
    ("turn everything off", True), ("all off", True), ("turn all off", True),
    ("off all aircons", False), ("off bedroom, that's all", False),
    ("turn off my aircon (I mean every aircon)", False),
])
async def test_all_opens_the_tv_gate_only_in_an_order(ctx, said, ok):
    assert HomeTools(ctx)._tv_named(ALICE, said) is ok


async def test_both_names_the_tv_only_in_a_two_device_home(tmp_path, ctx):
    assert HomeTools(ctx)._tv_named(ALICE, "off both") is False           # 3 devices: asks in code instead


async def test_notes_open_the_gate_only_through_a_room_or_device_word(ctx):
    await ctx.memory.add_fact(ALICE, "When I say 'both', I mean the bedroom aircon and the bedroom TV.", False)
    await ctx.memory.add_fact(ALICE, "The TV remote lives in the office drawer", True)
    h = HomeTools(ctx)
    assert h._tv_named(ALICE, "turn off my aircon now") is False          # "aircon" is a kind word
    assert h._tv_named(ALICE, "off bedroom") is True                      # the first note ties bedroom to a TV
    # a house note that only mentions a TV and a room is still a tie: the review's office case
    assert h._tv_named(ALICE, "off office") is True


async def test_send_anyway_has_no_undo(ctx):
    await (await prep(ctx, "set_device", {"device": "bed_tv", "power": False}, "tv off")).run(ALICE)
    with pytest.raises(AlreadyError) as e:
        await prep(ctx, "set_device", {"device": "bed_tv", "power": False}, "tv off")
    assert (await e.value.force.run(ALICE)).undo is None
    await (await prep(ctx, "set_device", {"device": "bed_ac", "power": False}, "off bedroom aircon")).run(ALICE)
    with pytest.raises(AlreadyError) as e:
        await prep(ctx, "set_device", {"device": "bed_ac", "power": False}, "off bedroom aircon")
    assert (await e.value.force.run(ALICE)).undo is None


async def test_a_mute_press_does_not_hide_the_last_power_send(ctx, monkeypatch):
    await (await prep(ctx, "set_device", {"device": "bed_tv", "power": True}, "tv on")).run(ALICE)
    ctx.devices.last["bed_tv"] = ctx.devices.last["bed_tv"].__class__(99, "mute", 0.0, "Bob", True, None)
    row = (await tools(ctx)["get_home_state"].run({}))["tvs"][0]
    assert row["bot_last_sent"] == "on"


def test_aircon_only_home_keeps_the_phase_9_tool_texts(tmp_path):
    c = make_ctx(tmp_path)
    t = tools(c)
    assert t["set_device"].description.startswith("Change one aircon now. Give only what the person asked for")
    assert t["get_home_state"].description == "What the bot last sent to each aircon, when, and by whom."
    assert "TV" not in c.assistant.system_prompt() and "each aircon right away" in c.assistant.system_prompt()


async def test_an_old_question_button_is_refused_and_a_typed_message_kills_it(ctx):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from iotbot.bot import handlers as hd
    from tests.test_bot import make_update
    from tests.test_chat import say, tg_context
    from tests.test_llm import FakeServer, completion

    ctx.assistant.llm = FakeServer([completion("ok")]).client()
    context = tg_context(ctx)
    u = say("off bedroom")
    await hd.on_text(u, context)
    old = u.effective_message.reply_text.call_args[1]["reply_markup"].inline_keyboard[1][0].callback_data
    await hd.on_text(say("what is the office set to?"), context)          # a new message: the buttons die
    t = make_update(ME, data=old)
    t.callback_query.message = SimpleNamespace(chat_id=ME, message_id=9)
    t.effective_message.reply_text = AsyncMock()
    await hd.on_button(t, context)
    t.callback_query.answer.assert_awaited_with("This button is no longer active.")
    t.effective_message.reply_text.assert_not_called()


async def test_a_tap_while_busy_keeps_the_question(ctx):
    from types import SimpleNamespace

    from iotbot.bot import handlers as hd
    from tests.test_bot import make_update
    from tests.test_chat import say, tg_context

    context = tg_context(ctx)
    u = say("off bedroom")
    await hd.on_text(u, context)
    data = u.effective_message.reply_text.call_args[1]["reply_markup"].inline_keyboard[0][0].callback_data
    ctx.assistant.in_flight.add(ME)
    t = make_update(ME, data=data)
    t.callback_query.message = SimpleNamespace(chat_id=ME, message_id=9)
    await hd.on_button(t, context)
    assert "Still working" in t.callback_query.answer.call_args[0][0]
    assert ctx.assistant.chats[ME].ask is not None


def test_a_note_about_my_aircon_skips_the_aircon_question(tmp_path):
    import asyncio
    c = make_ctx(tmp_path)
    asyncio.run(c.memory.add_fact(ALICE, "When I say 'my aircon', I mean the bedroom aircon.", False))
    from tests.test_llm import FakeServer, completion
    c.assistant.llm = FakeServer([completion("ok")]).client()
    out = asyncio.run(c.assistant.handle(ALICE, "turn off my aircon"))
    assert out.kind != "asked"
