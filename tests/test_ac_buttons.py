"""AC quick buttons (-1C / +1C / Swing / Fan) and the Settings card (Send now)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from iotbot.ac import mitsubishi as m
from iotbot.ac.state import AcState
from iotbot.bot import acpicker as ap
from iotbot.bot import handlers as hd
from iotbot.bot import keyboards as kb
from iotbot.bot.callbacks import encode
from tests.test_ac_service import ALICE, BEDROOM, OFFICE, frames_of, make_ctx
from tests.test_bot import ME, make_context, make_update


def sent_states(ctx):
    return [m.decode(frames_of(packets[0])[0]) for _, packets, _ in ctx.sent]


def labels(markup):
    return [[b.text for b in row] for row in markup.inline_keyboard]


async def tap(ctx, *parts):
    u = make_update(ME, data=encode(*parts))
    await hd.on_button(u, make_context(ctx))
    return u


def popup_of(u):
    (text,), kw = u.callback_query.answer.call_args
    return text, kw.get("show_alert", False)


def edited(u):
    (text,), kw = u.callback_query.edit_message_text.call_args
    return text, kw["reply_markup"]


async def test_ac_keyboard_layout(tmp_path):
    ctx = make_ctx(tmp_path)
    u = await tap(ctx, kb.KB, "d", "bed_ac")
    _, markup = edited(u)
    rows = labels(markup)
    assert rows[:3] == [["power on", "power off", "powerful"], ["-1C", "+1C", "Swing", "Fan"],
                        ["Settings", "Timer", "Schedule"]]


async def test_non_managed_ac_has_no_quick_buttons(tmp_path):
    ctx = make_ctx(tmp_path)
    _, markup = edited(await tap(ctx, kb.KB, "d", "old_ac"))
    flat = [t for row in labels(markup) for t in row]
    assert "+1C" not in flat and "Settings" not in flat


async def test_quick_button_never_turns_an_ac_on(tmp_path):
    ctx = make_ctx(tmp_path)
    text, alert = popup_of(await tap(ctx, kb.KB, "q", "bed_ac", "t+"))
    assert "Press On first" in text and alert
    await ctx.devices.run("bed_ac", "power_off", ALICE)
    ctx.sent.clear()
    text, _ = popup_of(await tap(ctx, kb.KB, "q", "bed_ac", "t+"))
    assert "Press On first" in text and not ctx.sent


async def test_temp_up_and_down(tmp_path):
    ctx = make_ctx(tmp_path)
    await ctx.devices.run("bed_ac", "power_on", ALICE)
    ctx.sent.clear()
    text, alert = popup_of(await tap(ctx, kb.KB, "q", "bed_ac", "t+"))
    assert text == "Sent bed_ac set: ON cool 23C fan 3 vane auto" and not alert
    await tap(ctx, kb.KB, "q", "bed_ac", "t-")
    await tap(ctx, kb.KB, "q", "bed_ac", "t-")
    assert [s.temp for s in sent_states(ctx)] == [23, 22, 21]
    assert ctx.ac.last("bed_ac").state == BEDROOM.with_changes(temp=21)


async def test_fast_double_tap_counts_twice(tmp_path):
    ctx = make_ctx(tmp_path)
    await ctx.devices.run("bed_ac", "power_on", ALICE)
    await asyncio.gather(tap(ctx, kb.KB, "q", "bed_ac", "t+"), tap(ctx, kb.KB, "q", "bed_ac", "t+"))
    assert ctx.ac.last("bed_ac").state.temp == 24


async def test_temp_at_limit_sends_nothing(tmp_path):
    ctx = make_ctx(tmp_path)
    await ctx.devices.send_ac_state("bed_ac", BEDROOM.with_changes(temp=31), ALICE)
    ctx.sent.clear()
    text, alert = popup_of(await tap(ctx, kb.KB, "q", "bed_ac", "t+"))
    assert "already" in text and not alert and not ctx.sent


async def test_swing_toggles_back_to_the_room_vane(tmp_path):
    ctx = make_ctx(tmp_path)
    await ctx.devices.run("office_ac", "power_on", ALICE)
    await tap(ctx, kb.KB, "q", "office_ac", "sw")
    assert ctx.ac.last("office_ac").state == OFFICE.with_changes(vane="swing")
    await tap(ctx, kb.KB, "q", "office_ac", "sw")
    assert ctx.ac.last("office_ac").state == OFFICE           # preset vane 1, not auto


async def test_fan_steps_round(tmp_path):
    ctx = make_ctx(tmp_path)
    await ctx.devices.run("bed_ac", "power_on", ALICE)
    for _ in range(6):
        await tap(ctx, kb.KB, "q", "bed_ac", "fn")
    assert [s.fan for s in sent_states(ctx)[1:]] == [4, "quiet", "auto", 1, 2, 3]


async def test_quick_button_ends_powerful(tmp_path):
    ctx = make_ctx(tmp_path)
    await ctx.devices.run("bed_ac", "powerful", ALICE)
    await tap(ctx, kb.KB, "q", "bed_ac", "t+")
    assert ctx.ac.last("bed_ac").state == BEDROOM.with_changes(temp=23, fan="auto")


async def test_kill_switch_hides_and_refuses_quick_buttons(tmp_path):
    ctx = make_ctx(tmp_path, env={"AC_ENCODER": "off"})
    _, markup = edited(await tap(ctx, kb.KB, "d", "bed_ac"))
    assert "+1C" not in [t for row in labels(markup) for t in row]
    text, _ = popup_of(await tap(ctx, kb.KB, "q", "bed_ac", "t+"))
    assert "no longer active" in text and not ctx.sent
    text, _ = popup_of(await tap(ctx, ap.NS, "!bed_ac", "223a0", "done"))
    assert "no longer active" in text and not ctx.sent


async def test_unknown_quick_op_refused(tmp_path):
    ctx = make_ctx(tmp_path)
    await ctx.devices.run("bed_ac", "power_on", ALICE)
    ctx.sent.clear()
    text, _ = popup_of(await tap(ctx, kb.KB, "q", "bed_ac", "zz"))
    assert "no longer active" in text and not ctx.sent


# ---- Settings card -----------------------------------------------------------------------

async def test_card_starts_from_preset_when_off_and_last_state_when_on(tmp_path):
    ctx = make_ctx(tmp_path)
    text, markup = edited(await tap(ctx, kb.KB, "set", "bed_ac"))
    assert text.startswith("bed_ac: cool 22C fan 3 vane auto") and "Send now" in text
    assert markup.inline_keyboard[-1][0].text == "Send now"
    await ctx.devices.send_ac_state("bed_ac", BEDROOM.with_changes(temp=25, vane="swing"), ALICE)
    text, _ = edited(await tap(ctx, kb.KB, "set", "bed_ac"))
    assert text.startswith("bed_ac: cool 25C fan 3 vane swing")


async def test_card_edits_without_sending_then_sends(tmp_path):
    ctx = make_ctx(tmp_path)
    u = await tap(ctx, ap.NS, "!bed_ac", "223a0", "t+")
    text, markup = edited(u)
    assert text.startswith("bed_ac: cool 23C") and not ctx.sent
    done = markup.inline_keyboard[-1][0].callback_data
    assert done == "ap:!bed_ac:233a0:done"
    text, alert = popup_of(await tap(ctx, *done.split(":")))
    assert text == "Sent bed_ac set: ON cool 23C fan 3 vane auto" and not alert
    assert ctx.ac.last("bed_ac").state == BEDROOM.with_changes(temp=23)


async def test_card_send_now_turns_an_off_ac_on(tmp_path):
    ctx = make_ctx(tmp_path)
    await ctx.devices.run("bed_ac", "power_off", ALICE)
    await tap(ctx, ap.NS, "!bed_ac", "242s0", "done")
    assert ctx.ac.last("bed_ac").state == AcState(power=True, temp=24, fan=2, vane="swing")


async def test_card_cancel_goes_back_to_the_device(tmp_path):
    ctx = make_ctx(tmp_path)
    text, markup = edited(await tap(ctx, ap.NS, "!bed_ac", "223a0", "x"))
    assert "bed_ac" in text and labels(markup)[1] == ["-1C", "+1C", "Swing", "Fan"]
    assert not ctx.sent


async def test_card_rejects_forged_state(tmp_path):
    ctx = make_ctx(tmp_path)
    for code in ("993a0", "22za0", "", "223a"):
        text, _ = popup_of(await tap(ctx, ap.NS, "!bed_ac", code, "done"))
        assert "no longer active" in text
    text, _ = popup_of(await tap(ctx, ap.NS, "!old_ac", "223a0", "done"))
    assert "no longer active" in text
    assert not ctx.sent


def test_card_callbacks_fit_for_the_longest_id():
    ap.picker_keyboard(hd.AC_CARD + "x" * 32, BEDROOM, "Send now")
    encode(kb.KB, "q", "x" * 32, "t+")


async def test_device_screen_shows_last_sent_state(tmp_path):
    ctx = make_ctx(tmp_path)
    text, _ = edited(await tap(ctx, kb.KB, "d", "bed_ac"))
    assert text == "Select bed_ac action\nNothing sent yet"
    text, _ = edited(await tap(ctx, kb.KB, "f", "bed_ac", "power_on"))
    assert text.endswith("Last sent: ON cool 22C fan 3 vane auto")
    text, _ = edited(await tap(ctx, kb.KB, "q", "bed_ac", "t+"))
    assert text.endswith("Last sent: ON cool 23C fan 3 vane auto")
    assert edited(await tap(ctx, kb.KB, "d", "old_ac"))[0] == "Select old_ac action"


async def test_unknown_state_wording(tmp_path):
    ctx = make_ctx(tmp_path)
    text, _ = popup_of(await tap(ctx, kb.KB, "q", "bed_ac", "sw"))
    assert text == "The bot has not sent anything to bed_ac yet. Press On first."


async def test_card_says_when_send_now_turns_the_ac_on(tmp_path):
    ctx = make_ctx(tmp_path)
    text, _ = edited(await tap(ctx, kb.KB, "set", "bed_ac"))
    assert "Send now turns it on" in text
    await ctx.devices.run("bed_ac", "power_on", ALICE)
    text, _ = edited(await tap(ctx, kb.KB, "set", "bed_ac"))
    assert "turns it on" not in text


async def test_send_now_leaves_no_send_button_behind(tmp_path):
    ctx = make_ctx(tmp_path)
    text, markup = edited(await tap(ctx, ap.NS, "!bed_ac", "242s0", "done"))
    assert text.endswith("Last sent: ON cool 24C fan 2 vane swing")
    assert "Send now" not in [t for row in labels(markup) for t in row]


async def test_screen_refresh_after_a_send_is_best_effort(tmp_path):
    from telegram import InaccessibleMessage
    from telegram.error import BadRequest
    ctx = make_ctx(tmp_path)
    await ctx.devices.run("bed_ac", "power_on", ALICE)
    u = make_update(ME, data=encode(kb.KB, "q", "bed_ac", "t+"))
    u.callback_query.edit_message_text.side_effect = BadRequest("Message to edit not found")
    await hd.on_button(u, make_context(ctx))                 # no raise: the send already succeeded
    assert ctx.ac.last("bed_ac").state.temp == 23
    u = make_update(ME, data=encode(kb.KB, "q", "bed_ac", "t+"))
    u.callback_query.message = InaccessibleMessage(chat=None, message_id=1)
    sent = AsyncMock()
    u.callback_query.get_bot = lambda: SimpleNamespace(send_message=sent)
    await hd.on_button(u, make_context(ctx))
    sent.assert_not_awaited()                                # an old keyboard gets no new message
