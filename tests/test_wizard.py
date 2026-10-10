from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from iotbot.ac.state import AcState
from iotbot.bot import handlers as hd
from iotbot.bot.wizard import parse_time
from iotbot.schedule import model as sm
from tests.test_ac_service import make_ctx
from tests.test_bot import ME, make_update, replied

SGT = ZoneInfo("Asia/Singapore")
T0 = datetime(2026, 10, 10, 12, 0, tzinfo=SGT)     # Sat noon


@pytest.mark.parametrize("raw,want", [
    ("23:00", "23:00"), ("2330", "23:30"), ("23", "23:00"), ("11pm", "23:00"), ("11:30 pm", "23:30"),
    ("7am", "07:00"), ("12am", "00:00"), ("12pm", "12:00"), ("7.30", "07:30"), (" 0:05 ", "00:05"),
    ("24:00", None), ("13pm", None), ("7:60", None), ("tonight", None), ("", None), ("123456", None)])
def test_parse_time(raw, want):
    assert parse_time(raw) == want


class Bot:
    def __init__(self, tmp_path):
        self.ctx = make_ctx(tmp_path)
        self.ctx.schedules._clock = lambda: T0.timestamp()
        self.context = SimpleNamespace(args=[], error=None, application=SimpleNamespace(bot_data={"ctx": self.ctx}),
                                       bot=SimpleNamespace(edit_message_text=AsyncMock()))
        self.screen = None

    async def cmd(self, *args):
        self.context.args = list(args)
        u = make_update(ME)
        await hd.cmd_schedule(u, self.context)
        call = u.effective_message.reply_text.call_args
        self.screen = (call.args[0], call.kwargs.get("reply_markup"))
        return replied(u)

    async def tap(self, label=None, data=None):
        if data is None:
            data = self.button(label)
        u = make_update(ME, data=data)
        u.callback_query.message = SimpleNamespace(chat_id=ME, message_id=5)
        await hd.on_button(u, self.context)
        if u.callback_query.edit_message_text.call_args:
            call = u.callback_query.edit_message_text.call_args
            self.screen = (call.args[0], call.kwargs.get("reply_markup"))
        answer = u.callback_query.answer.call_args
        return answer.args[0] if answer and answer.args else ""

    def button(self, label):
        for row in self.screen[1].inline_keyboard:
            for b in row:
                if b.text == label:
                    return b.callback_data
        raise AssertionError(f"no button {label!r} in {[[b.text for b in r] for r in self.screen[1].inline_keyboard]}")

    @property
    def text(self):
        return self.screen[0]

    async def say(self, text):
        u = make_update(ME)
        u.effective_message.text = text
        await hd.on_text(u, self.context)
        return u


@pytest.fixture
def bot(tmp_path):
    return Bot(tmp_path)


async def test_new_weekly_on_with_picker(bot):
    await bot.cmd()
    assert "none yet" in bot.text
    await bot.tap("+ New")
    await bot.tap("bed_ac")
    await bot.tap("Turn on")
    assert bot.text.startswith("bed_ac: cool 22C fan 3 vane auto")       # the room preset
    await bot.tap("+")
    await bot.tap("4")
    await bot.tap("swing")
    assert "cool 23C fan 4 vane swing" in bot.text
    await bot.tap("Next")
    await bot.tap("Every week")
    await bot.tap("23:00")
    await bot.tap("Weekdays")
    await bot.tap("[Fr]")                                 # toggle Friday off
    await bot.tap("Su")
    await bot.tap("Next")
    assert "Preview" in bot.text and "23:00 Sun-Thu" in bot.text and "Next: tomorrow 23:00" in bot.text
    await bot.tap("Save")
    assert bot.text.startswith("Saved s")
    (s,) = bot.ctx.schedules.store.all().values()
    assert s.action == sm.On(AcState(power=True, temp=23, fan=4, vane="swing"))
    assert s.when.days == ("mon", "tue", "wed", "thu", "sun") and s.created_by == ME


async def test_typed_time_and_once_off(bot):
    await bot.cmd()
    await bot.tap("+ New")
    await bot.tap("bed_ac")
    await bot.tap("Turn off")
    await bot.tap("Once (timer)")
    note = await bot.tap("Type a time")
    assert "Send the time" in note
    u = await bot.say("nonsense")
    assert "not a time, so I stopped waiting" in replied(u)
    u = await bot.say("1:30am")                       # no longer waiting
    assert not u.effective_message.reply_text.called and not bot.context.bot.edit_message_text.called
    await bot.tap("Type a time")
    await bot.say("1:30am")
    (text,), kw = bot.context.bot.edit_message_text.call_args
    assert "Preview" in text and "once, tomorrow 01:30" in text
    assert kw["chat_id"] == ME and kw["message_id"] == 5
    bot.screen = (text, kw["reply_markup"])
    await bot.tap("Save")
    (t,) = bot.ctx.schedules.store.all().values()
    assert t.is_timer and t.action == sm.Off() and t.when.at == datetime(2026, 10, 11, 1, 30, tzinfo=SGT)


async def test_text_without_a_waiting_draft_is_ignored(bot):
    u = await bot.say("hello")
    assert not u.effective_message.reply_text.called


async def test_adjust_and_capture_paths(bot):
    await bot.cmd()
    await bot.tap("+ New")
    await bot.tap("bed_ac")
    await bot.tap("Change temp")
    await bot.tap("25C")
    await bot.tap("Every week")
    await bot.tap("07:00")
    await bot.tap("Every day")
    await bot.tap("Next")
    await bot.tap("Save (from next time)")               # 07:00 already passed today
    await bot.tap("<- Menu")
    await bot.tap("+ New")
    await bot.tap("old_ac")
    await bot.tap("power on")
    await bot.tap("Once (timer)")
    await bot.tap("21:30")
    await bot.tap("Save")
    kinds = sorted(type(s.action).__name__ for s in bot.ctx.schedules.store.all().values())
    assert kinds == ["Adjust", "Capture"]


async def test_back_and_cancel(bot):
    await bot.cmd()
    await bot.tap("+ New")
    await bot.tap("bed_ac")
    await bot.tap("Turn off")
    await bot.tap("<- Back")
    assert "do what?" in bot.text
    await bot.tap("Cancel")
    assert bot.text == "Cancelled." and bot.ctx.schedules.store.all() == {}


async def test_conflict_keep_both(bot):
    for i in range(2):
        await bot.cmd()
        await bot.tap("+ New")
        await bot.tap("bed_ac")
        await bot.tap("Turn off")
        await bot.tap("Every week")
        await bot.tap("23:00")
        await bot.tap("Every day")
        await bot.tap("Next")
        if i == 0:
            await bot.tap("Save")
    assert "Clashes with" in bot.text
    await bot.tap("Keep both")
    assert len(bot.ctx.schedules.store.all()) == 2


async def test_passed_today_offers_run_now(bot):
    await bot.cmd()
    await bot.tap("+ New")
    await bot.tap("bed_ac")
    await bot.tap("Turn off")
    await bot.tap("Every week")
    await bot.tap("07:00")
    await bot.tap("Weekends")
    await bot.tap("Next")
    assert "already passed today" in bot.text
    await bot.tap("Save + run now")
    assert "Done: bed_ac off" in bot.text and bot.ctx.sent


async def test_card_skip_pause_delete_undo(bot):
    await test_new_weekly_on_with_picker(bot)
    await bot.tap("Open")
    assert "Next: tomorrow 23:00" in bot.text
    await bot.tap("Skip next")
    assert "Skipping" in bot.text and "Skipping: Sun 11 Oct" in bot.text
    await bot.tap("Pause")
    assert "Paused until you resume it." in bot.text
    await bot.tap("Resume")
    await bot.tap("Delete")
    await bot.tap("Yes, delete")
    assert bot.ctx.schedules.store.all() == {}
    await bot.tap("Undo")
    assert "Reverted" in bot.text and len(bot.ctx.schedules.store.all()) == 1


async def test_stale_card_button(bot):
    await test_new_weekly_on_with_picker(bot)
    await bot.tap("Open")
    skip = bot.button("Skip next")
    await bot.tap(data=skip)
    note = await bot.tap(data=skip)
    assert "double tap" in note
    (s,) = bot.ctx.schedules.store.all().values()
    assert len(s.skip_dates) == 1


async def test_edit_time(bot):
    await test_new_weekly_on_with_picker(bot)
    await bot.tap("Open")
    await bot.tap("Edit time")
    await bot.tap("22:30")
    assert "on which days" in bot.text                # prefilled; days can change too
    await bot.tap("Next")
    assert "Preview" in bot.text and "22:30 Sun-Thu" in bot.text
    await bot.tap("Save")
    (s,) = bot.ctx.schedules.store.all().values()
    assert s.when.time == "22:30" and s.rev == 2 and s.action.state.temp == 23


async def test_edit_settings(bot):
    await test_new_weekly_on_with_picker(bot)
    await bot.tap("Open")
    await bot.tap("Edit settings")
    assert "cool 23C fan 4" in bot.text
    await bot.tap("-")
    await bot.tap("Next")
    await bot.tap("Save")
    (s,) = bot.ctx.schedules.store.all().values()
    assert s.action.state.temp == 22 and s.when.time == "23:00"


async def test_tonight(bot):
    await test_new_weekly_on_with_picker(bot)
    await bot.tap("<- Menu")
    await bot.tap("Tonight")
    assert "Nothing scheduled" in bot.text                # Sat is not in Sun-Thu


async def test_expired_draft(bot):
    await bot.cmd()
    await bot.tap("+ New")
    dev = bot.button("bed_ac")
    bot.context.application.bot_data["wizard"].drafts.clear()
    before = bot.text
    note = await bot.tap(data=dev)
    assert "expired" in note and bot.text == before     # popup only


async def test_device_keyboard_has_timer_and_schedule(bot):
    u = make_update(ME, data="kb:d:bed_ac")
    await hd.on_button(u, bot.context)
    markup = u.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "Timer" in labels and "Schedule" in labels


async def test_quick_timer_two_taps(bot):
    bot.screen = ("", None)
    await bot.tap(data="sw:-:tm:bed_ac")
    await bot.tap("1h")
    assert bot.text.startswith("bed_ac off at today 13:00 (in 1h)")
    (t,) = bot.ctx.schedules.store.all().values()
    assert t.is_timer and t.when.at == datetime(2026, 10, 10, 13, 0, tzinfo=SGT)
    await bot.tap("Undo")
    assert bot.ctx.schedules.store.all() == {}


async def test_timer_at_a_time_saves_after_the_time(bot):
    bot.screen = ("", None)
    await bot.tap(data="sw:-:tm:bed_ac")
    await bot.tap("At a time...")
    await bot.tap("00:00")
    assert bot.text.startswith("bed_ac off at tomorrow 00:00")
    assert len(bot.ctx.schedules.store.all()) == 1


async def test_schedule_from_device_keyboard(bot):
    bot.screen = ("", None)
    await bot.tap(data="sw:-:newfor:bed_ac")
    assert "bed_ac: do what?" in bot.text


# ---- one-liner (2.9) ----------------------------------------------------------------------

@pytest.mark.parametrize("word,days", [
    ("sun-thu", ["mon", "tue", "wed", "thu", "sun"]), ("fri-mon", ["mon", "fri", "sat", "sun"]),
    ("mon,wed,fri", ["mon", "wed", "fri"]), ("weekdays", ["mon", "tue", "wed", "thu", "fri"]),
    ("weekends", ["sat", "sun"]), ("daily", list(sm.DAYS)), ("Friday", ["fri"]), ("sun-sun", ["sun"]),
    ("funday", None), ("mon-xyz", None), ("", None)])
def test_parse_days(word, days):
    from iotbot.bot.oneliner import parse_days
    assert parse_days(word) == days


async def test_add_weekly_on_from_room(bot):
    text = await bot.cmd("add", "bedroom", "on", "23:00", "sun-thu", "cool", "22", "fan", "4", "name=Bedtime")
    assert "Preview" in text and "Bedtime: bed_ac on, cool 22C fan 4 vane auto, 23:00 Sun-Thu" in text
    await bot.tap("Save")
    (s,) = bot.ctx.schedules.store.all().values()
    assert s.label == "Bedtime" and s.action.state.fan == 4


async def test_add_picks_the_managed_ac_in_a_room(bot):
    assert "office_ac on" in await bot.cmd("add", "office", "on", "9am")
    assert "office_ac off" in await bot.cmd("add", "office", "off", "7am", "weekdays")


async def test_add_capture_tomorrow(bot):
    text = await bot.cmd("add", "old_ac", "power_on", "21:00", "tomorrow")
    assert "old_ac power on, once, tomorrow 21:00" in text


async def test_add_in_two_hours(bot):
    text = await bot.cmd("add", "bedroom", "off", "in", "2h")
    assert "bed_ac off, once, today 14:00" in text
    await bot.tap("Save")
    (t,) = bot.ctx.schedules.store.all().values()
    assert t.when.at == datetime(2026, 10, 10, 14, 0, tzinfo=SGT)


async def test_add_adjust_with_quoted_name(bot):
    text = await bot.cmd("add", "bedroom", "set", "25", "14:00", "daily", 'name="Hot', 'day"')
    assert "Hot day: bed_ac set temp 25, 14:00 every day" in text


@pytest.mark.parametrize("args,err", [
    (["nowhere", "on", "23:00"], "No device or room"),
    (["bedroom", "on", "23:00", "fan", "9"], "fan must be"),
    (["bedroom", "on", "23:00", "loud"], "Do not understand 'loud'"),
    (["bedroom", "on", "soon"], "Give a time"),
    (["bedroom", "set", "23:00"], "set needs a temperature"),
    (["bedroom", "off", "23:00", "22"], "settings only go with on"),
    (["old_ac", "set", "25", "23:00"], "cannot change temperature"),
    (["old_ac", "dance", "23:00"], "has no 'dance'"),
    (["bedroom", "on"], "Usage"),
])
async def test_add_errors(bot, args, err):
    assert err in await bot.cmd("add", *args)


async def test_subcommands(bot):
    assert "Nothing scheduled" in await bot.cmd("tonight")
    assert "None yet" in await bot.cmd("list")
    assert "Usage" in await bot.cmd("help")


# ---- review fixes (2.6-2.8) ---------------------------------------------------------------

async def test_double_tap_timer_makes_one(bot):
    import asyncio
    bot.screen = ("", None)
    await bot.tap(data="sw:-:tm:bed_ac")
    one_h = bot.button("1h")
    await asyncio.gather(bot.tap(data=one_h), bot.tap(data=one_h))
    assert len(bot.ctx.schedules.store.all()) == 1


async def test_forged_timer_minutes_refused(bot):
    bot.screen = ("", None)
    note = await bot.tap(data="sw:-:tmin:bed_ac:99999999")
    assert "no longer active" in note and bot.ctx.schedules.store.all() == {}


async def test_tap_ends_type_a_time(bot):
    await bot.cmd()
    await bot.tap("+ New")
    await bot.tap("bed_ac")
    await bot.tap("Turn off")
    await bot.tap("Every week")
    await bot.tap("Type a time")
    await bot.tap("22:00")                             # changed their mind
    u = await bot.say("7")
    assert not u.effective_message.reply_text.called
    assert bot.context.application.bot_data["wizard"].drafts and "on which days" in bot.text


async def test_strangers_text_gets_no_reply(bot):
    u = make_update(999)
    u.effective_message.text = "hello"
    await hd.on_text(u, bot.context)
    assert not u.effective_message.reply_text.called


async def test_double_tap_save_keeps_the_saved_message(bot):
    await bot.cmd("add", "bedroom", "off", "23:00", "daily")
    save = bot.button("Save")
    await bot.tap(data=save)
    saved = bot.text
    note = await bot.tap(data=save)
    assert bot.text == saved and "expired" in note and len(bot.ctx.schedules.store.all()) == 1


async def test_double_tap_undo_keeps_the_reverted_message(bot):
    await bot.cmd("add", "bedroom", "off", "23:00", "daily")
    await bot.tap("Save")
    undo = bot.button("Undo")
    await bot.tap(data=undo)
    reverted = bot.text
    await bot.tap(data=undo)
    assert bot.text == reverted and "Reverted" in reverted


async def test_double_taps_on_steps_and_days(bot):
    await bot.cmd()
    await bot.tap("+ New")
    await bot.tap("bed_ac")
    off = bot.button("Turn off")
    await bot.tap(data=off)
    await bot.tap(data=off)                             # stale: ignored
    await bot.tap("Every week")
    await bot.tap("23:00")
    mo = bot.button("Mo")
    await bot.tap(data=mo)
    await bot.tap(data=mo)                              # sets, does not toggle back
    assert "[Mo]" in [b.text for r in bot.screen[1].inline_keyboard for b in r]
    await bot.tap("<- Back")
    await bot.tap("<- Back")
    assert bot.text == "When?"                          # one Back per step


async def test_broken_entry_with_odd_id_does_not_break_the_list(bot):
    bot.ctx.schedules.store.broken["x:" + "y" * 80] = "odd"
    await bot.cmd("list")
    assert "remove it from schedules.json by hand" in bot.text


async def test_edit_settings_keeps_a_far_timer_date(bot):
    await bot.cmd("add", "bedroom", "on", "21:00", "tomorrow")
    await bot.tap("Save")
    (t,) = bot.ctx.schedules.store.all().values()
    await bot.tap("Open")
    await bot.tap("Edit settings")
    await bot.tap("+")
    await bot.tap("Next")
    await bot.tap("Save")
    (t2,) = bot.ctx.schedules.store.all().values()
    assert t2.when == t.when and t2.action.state.temp == 23


async def test_timer_cancel_returns_to_the_device(bot):
    bot.screen = ("", None)
    await bot.tap(data="sw:-:tm:bed_ac")
    assert bot.button("Cancel") == "kb:d:bed_ac"


async def test_list_is_capped(bot):
    from iotbot.bot import wizard as wz
    for i in range(wz.MAX_LIST + 3):
        await bot.cmd("add", "office", "off", f"{i // 6:02d}:{i % 6 * 10:02d}", "mon")
        await bot.tap("Save")
    await bot.cmd("list")
    assert "... and 3 more" in bot.text
    assert sum(len(r) for r in bot.screen[1].inline_keyboard) <= 100


# ---- review fixes (2.9) -------------------------------------------------------------------

async def test_today_in_the_past_is_an_error(bot):
    assert "already passed today" in await bot.cmd("add", "bedroom", "off", "09:00", "today")
    assert "today 13:00" in await bot.cmd("add", "bedroom", "off", "13:00", "today")


async def test_room_with_only_a_plain_device(bot):
    room = bot.ctx.registry.rooms["office"]
    room.devices[:] = [d for d in room.devices if d != "office_ac"]
    assert "old_ac power on" in await bot.cmd("add", "office", "on", "20:00")


async def test_apostrophe_and_case(bot):
    text = await bot.cmd("add", "BED_AC", "off", "23:00", "name=Bob's")
    assert "Bob's: bed_ac off" in text or "Bob&#x27;s" in text


async def test_button_verb_in_a_room(bot):
    # office_ac has a captured power_on too, so the room is ambiguous for that button
    assert "name one: office_ac, old_ac" in await bot.cmd("add", "office", "power_on", "20:00")
    assert "Nothing in room 'office' can do 'dance'" in await bot.cmd("add", "office", "dance", "20:00")
