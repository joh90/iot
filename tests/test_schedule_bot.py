import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from iotbot.ac.state import AcState
from iotbot.bot import handlers as hd
from iotbot.bot.callbacks import encode
from iotbot.result import Actor
from iotbot.schedule.notify import NS, Notice
from tests.test_ac_service import make_ctx
from tests.test_bot import ME, make_context, make_update, replied

ON22 = {"kind": "on", "state": AcState(power=True, temp=22, fan=3).to_dict()}
ALICE = Actor(ME, "Alice", "button")


async def add(ctx, **spec):
    spec = {"device": "bed_ac", "action": ON22,
            "when": {"kind": "weekly", "time": "23:00", "days": ["mon", "tue", "wed", "thu", "sun"]}, **spec}
    p = ctx.schedules.plan(ALICE, spec)
    r = await ctx.schedules.apply(ALICE, p.data["plan_id"], "keep_both")
    assert r.ok, r.message
    return r.data["schedule"]


def test_context_wires_schedules(tmp_path):
    ctx = make_ctx(tmp_path)
    assert ctx.scheduler.notify == ctx.notifier.on_fire
    assert ctx.schedules.on_change == ctx.scheduler.wake
    assert ctx.notifier.recipients(ME) == [ME] and ctx.notifier.recipients(42) == [ME]


def test_broken_schedule_is_a_config_warning(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "schedules.json").write_text(json.dumps({"version": 1, "schedules": {"s0bad": {}}}))
    ctx = make_ctx(tmp_path)
    assert any("s0bad is broken" in w for w in ctx.warnings)


async def test_status_shows_schedules(tmp_path):
    ctx = make_ctx(tmp_path)
    await add(ctx, label="Bedtime")
    u = make_update(ME)
    await hd.cmd_status(u, make_context(ctx))
    text = replied(u)
    assert "Schedules</b>: 1 weekly, 0 timers, 0 paused" in text
    assert "Next: Bedtime: bed_ac on, cool 22C fan 3 vane auto" in text
    ctx.scheduler.clock_state = "waiting for NTP"
    u = make_update(ME)
    await hd.cmd_status(u, make_context(ctx))
    assert "Scheduler: waiting for NTP" in replied(u)


async def test_skip_button_and_double_tap(tmp_path):
    ctx = make_ctx(tmp_path)
    s = await add(ctx)
    u = make_update(ME, data=encode(NS, "skip", s.id, s.rev))
    await hd.on_button(u, make_context(ctx))
    (text,), kw = u.callback_query.answer.call_args
    assert text.startswith("Skipping") and not kw["show_alert"]
    u = make_update(ME, data=encode(NS, "skip", s.id, s.rev))
    await hd.on_button(u, make_context(ctx))
    (text,), kw = u.callback_query.answer.call_args
    assert "double tap" in text and kw["show_alert"]
    assert len(ctx.schedules.get(s.id).skip_dates) == 1


async def test_pause_button(tmp_path):
    ctx = make_ctx(tmp_path)
    s = await add(ctx)
    u = make_update(ME, data=encode(NS, "pause", s.id, s.rev))
    await hd.on_button(u, make_context(ctx))
    assert not ctx.schedules.get(s.id).enabled


async def test_expired_undo_button(tmp_path):
    ctx = make_ctx(tmp_path)
    u = make_update(ME, data=encode(NS, "undo", "deadbeef"))
    await hd.on_button(u, make_context(ctx))
    (text,), kw = u.callback_query.answer.call_args
    assert "10 minutes" in text and kw["show_alert"]


async def test_send_notice_silent_and_buttons():
    bot = SimpleNamespace(send_message=AsyncMock())
    await hd.send_notice(bot, Notice(5, "hi", [[("Undo", "sc:undo:x")]], silent=True))
    (uid, text), kw = bot.send_message.call_args
    assert (uid, text, kw["disable_notification"]) == (5, "hi", True)
    assert kw["reply_markup"].inline_keyboard[0][0].callback_data == "sc:undo:x"
    await hd.send_notice(bot, Notice(5, "FAILED", silent=False))
    assert bot.send_message.call_args.kwargs["disable_notification"] is False
    assert bot.send_message.call_args.kwargs["reply_markup"] is None


async def test_app_starts_and_stops_the_scheduler(tmp_path, monkeypatch):
    from iotbot.bot.app import build_application
    ctx = make_ctx(tmp_path)
    started = []
    monkeypatch.setattr(ctx.scheduler, "start", lambda: started.append(1))
    stop = AsyncMock()
    monkeypatch.setattr(ctx.scheduler, "stop", stop)
    app = build_application(ctx)
    monkeypatch.setattr(type(app.bot), "set_my_commands", AsyncMock())
    await app.post_init(app)
    assert started == [1] and ctx.notifier.send is not None
    await app.post_stop(app)
    stop.assert_awaited_once()


async def test_removing_a_user_hands_over_their_schedules(tmp_path):
    from iotbot.bot import keyboards as kb
    ctx = make_ctx(tmp_path)
    bob = Actor(222, "Bob", "button")
    await ctx.users.add(ALICE, 222, "Bob")
    p = ctx.schedules.plan(bob, {"device": "bed_ac", "action": ON22, "label": "Bobs",
                                 "when": {"kind": "weekly", "time": "23:00", "days": ["sun"]}})
    s = (await ctx.schedules.apply(bob, p.data["plan_id"])).data["schedule"]
    c = make_context(ctx)
    u = make_update(ME, data=encode(kb.US, "ask", 222))
    await hd.on_button(u, c)
    assert "Their 1 schedule(s) (Bobs) will be handed to you" in u.callback_query.edit_message_text.call_args.args[0]
    u = make_update(ME, data=encode(kb.US, "del", 222))
    await hd.on_button(u, c)
    (text,), _ = u.callback_query.answer.call_args
    assert "1 schedule(s) are now yours" in text
    assert ctx.schedules.get(s.id).created_by == ME



async def test_handover_picks_up_orphans_and_survives_a_fired_timer(tmp_path):
    from dataclasses import replace
    ctx = make_ctx(tmp_path)
    bob = Actor(222, "Bob", "button")
    p = ctx.schedules.plan(bob, {"device": "bed_ac", "action": ON22,
                                 "when": {"kind": "weekly", "time": "23:00", "days": ["sun"]}})
    s = (await ctx.schedules.apply(bob, p.data["plan_id"])).data["schedule"]   # Bob was never approved
    r = await ctx.schedules.reassign(ALICE, ctx.users.is_allowed, ME)
    new = ctx.schedules.get(s.id)
    assert r.ok and r.data["ids"] == [s.id] and new.created_by == ME and new.rev == s.rev
    assert (await ctx.schedules.reassign(ALICE, ctx.users.is_allowed, ME)).data["ids"] == []
