"""Turns log (Phase 6.1-6.3): one line per update or schedule fire."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from telegram import Update, User
from telegram.ext import Application, CommandHandler, MessageHandler, TypeHandler, filters

from iotbot import telemetry
from iotbot.bot import backlog as bl
from iotbot.bot import handlers as hd
from iotbot.bot.app import TrackedApplication, button_intent, turn_fields
from iotbot.store import JsonlLog
from tests.test_ac_service import ALICE, make_ctx
from tests.test_bot import ME, make_context, make_update
from tests.test_schedule_runner import FIRE, env, sched  # noqa: F401 -- env is a fixture

NOW = datetime.now(UTC).replace(microsecond=0)   # the offline gate reads the real clock
STRANGER = 99


def turns(tmp_path):
    return [json.loads(line) for f in sorted((tmp_path / "logs").glob("turns-*"))
            for line in f.read_text().splitlines()]


class Steps:
    """perf_counter stand-in: each call advances by `step` seconds."""

    def __init__(self, step=0.25):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


def tracker(tmp_path, step=0.25):
    return telemetry.Tracker(JsonlLog(tmp_path / "logs", "turns"), clock=Steps(step))


# ---- Tracker ----------------------------------------------------------------------------

async def test_turn_written_with_time_and_default_outcome(tmp_path):
    async with tracker(tmp_path).turn(surface="slash", raw="/ping", intent="/ping"):
        pass
    (t,) = turns(tmp_path)
    assert (t["surface"], t["raw"], t["outcome"], t["total_ms"]) == ("slash", "/ping", "ok", 250)
    assert t["llm_ms"] is None and "ts" in t


async def test_crash_is_logged_and_reraised(tmp_path):
    with pytest.raises(ZeroDivisionError):
        async with tracker(tmp_path).turn(surface="text"):
            raise ZeroDivisionError
    (t,) = turns(tmp_path)
    assert (t["outcome"], t["error"]) == ("error", "ZeroDivisionError")


async def test_cancelled_turn_still_written(tmp_path):
    started = asyncio.Event()

    async def slow():
        async with tracker(tmp_path).turn(surface="button"):
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(slow())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert turns(tmp_path)[0]["outcome"] == "cancelled"


async def test_long_raw_is_cut(tmp_path):
    async with tracker(tmp_path).turn(surface="text", raw="x" * 5000):
        pass
    assert len(turns(tmp_path)[0]["raw"]) == telemetry.MAX_RAW + 3


async def test_note_outside_a_turn_is_a_no_op():
    telemetry.note(outcome="x")
    telemetry.note_send("bed_ac", 5, True, False)
    assert telemetry.current() is None


async def test_concurrent_turns_do_not_mix(tmp_path):
    tr = tracker(tmp_path)

    async def one(name, device):
        async with tr.turn(surface="button", name=name):
            await asyncio.sleep(0)
            telemetry.note_send(device, 10, True, False)
            await asyncio.sleep(0)

    await asyncio.gather(one("a", "bed_ac"), one("b", "office_ac"))
    assert {t["name"]: t["devices"] for t in turns(tmp_path)} == {"a": ["bed_ac"], "b": ["office_ac"]}


# ---- what the device service adds -----------------------------------------------------

async def test_device_send_adds_ir_time_devices_and_outcome(tmp_path):
    ctx = make_ctx(tmp_path)
    async with ctx.tracker.turn(surface="slash"):
        await ctx.devices.run("bed_ac", "power_on", ALICE)
    (t,) = turns(tmp_path)
    assert (t["outcome"], t["sends"], t["devices"], t["maybe_delivered"]) == ("ok", 1, ["bed_ac"], False)
    assert t["ir_ms"] >= 0


async def test_refused_call_outcome_is_the_error_code(tmp_path):
    ctx = make_ctx(tmp_path)
    async with ctx.tracker.turn(surface="button"):
        await ctx.devices.adjust_ac_state("bed_ac", {"temp": 25}, ALICE)
    (t,) = turns(tmp_path)
    assert (t["outcome"], t["sends"]) == ("ac_state_unknown", 0)


async def test_failed_send_marks_maybe_delivered(tmp_path):
    from iotbot.devices.hub import HubError
    ctx = make_ctx(tmp_path)
    ctx.fail = HubError("no reply", maybe_delivered=True)
    async with ctx.tracker.turn(surface="slash"):
        await ctx.devices.run("bed_ac", "power_on", ALICE)
    (t,) = turns(tmp_path)
    assert (t["outcome"], t["maybe_delivered"], t["sends"]) == ("send_failed", True, 1)


# ---- denied / error / offline drop -----------------------------------------------------

async def test_stranger_is_denied(tmp_path):
    ctx = make_ctx(tmp_path)
    async with ctx.tracker.turn(surface="slash"):
        await hd.cmd_on(make_update(STRANGER), make_context(ctx, ["bed_ac"]))
    assert turns(tmp_path)[0]["outcome"] == "denied" and not ctx.sent


async def test_error_handler_marks_the_turn(tmp_path):
    ctx = make_ctx(tmp_path)
    c = make_context(ctx)
    c.error = KeyError("x")
    async with ctx.tracker.turn(surface="button"):
        await hd.on_error(make_update(ME, data="kb:x"), c)
    assert (turns(tmp_path)[0]["outcome"], turns(tmp_path)[0]["error"]) == ("error", "KeyError")


# ---- turn fields from a Telegram update -------------------------------------------------

def tg_update(uid, *, text=None, data=None, user=ME, age_s=0, first="Alice", bot=None):
    u = {"id": user, "is_bot": False, "first_name": first}
    msg = {"message_id": 1, "date": int((NOW - timedelta(seconds=age_s)).timestamp()),
           "chat": {"id": user, "type": "private"}, "from": u}
    if text is not None:
        msg["text"] = text
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if data is not None:
        return Update.de_json({"update_id": uid, "callback_query": {
            "id": "q", "from": u, "chat_instance": "c", "data": data, "message": msg}}, bot)
    return Update.de_json({"update_id": uid, "message": msg}, bot)


def test_turn_fields_slash_text_button(tmp_path):
    ctx = make_ctx(tmp_path)
    now = NOW.timestamp()
    f = turn_fields(tg_update(5, text="/on@home_bot bed_ac", age_s=3), ctx, now)
    assert (f["surface"], f["intent"], f["lag_s"], f["name"], f["allowed"]) == ("slash", "/on", 3, "Alice", True)
    assert f["request_id"] == "tg:5" and f["raw"] == "/on@home_bot bed_ac"
    f = turn_fields(tg_update(6, text="off the aircon lah"), ctx, now)
    assert (f["surface"], f["intent"], f["raw"]) == ("text", "text", "off the aircon lah")
    f = turn_fields(tg_update(7, data="kb:q:bed_ac:t+"), ctx, now)
    assert (f["surface"], f["intent"], f["raw"], f["lag_s"]) == ("button", "kb:q", "kb:q:bed_ac:t+", None)


def test_turn_fields_stranger_keeps_telegram_name(tmp_path):
    f = turn_fields(tg_update(8, text="hello", user=STRANGER, first="Mallory"), make_ctx(tmp_path), NOW.timestamp())
    assert (f["user_id"], f["name"], f["allowed"]) == (STRANGER, "Mallory", False)


# ---- the real PTB path ------------------------------------------------------------------

def tracked_app(ctx, *handlers):
    app = Application.builder().application_class(TrackedApplication).token("1:" + "x" * 35).build()
    app.bot_data["ctx"] = ctx
    for group, h in handlers:
        app.add_handler(h, group=group)
    app._initialized = True          # no network: initialize() would call getMe
    app.bot._bot_user = User(1, "bot", True, username="home_bot")
    return app


async def test_process_update_writes_one_line_per_update(tmp_path):
    ctx = make_ctx(tmp_path)

    async def on(update, context):
        await ctx.devices.run("bed_ac", "power_on", ALICE)

    async def boom(update, context):
        raise RuntimeError("bad")

    async def on_error(update, context):
        telemetry.note(outcome="error", error=context.error.__class__.__name__)

    app = tracked_app(ctx, (0, CommandHandler("on", on)), (0, MessageHandler(filters.TEXT & ~filters.COMMAND, boom)))
    app.add_error_handler(on_error)
    await app.process_update(tg_update(1, text="/on bed_ac", bot=app.bot))
    await app.process_update(tg_update(2, text="hi", bot=app.bot))
    one, two = turns(tmp_path)
    assert (one["intent"], one["outcome"], one["devices"], one["request_id"]) == ("/on", "ok", ["bed_ac"], "tg:1")
    assert (two["surface"], two["outcome"], two["error"]) == ("text", "error", "RuntimeError")


async def test_offline_drop_is_logged(tmp_path):
    ctx = make_ctx(tmp_path)
    app = tracked_app(ctx, (-1, TypeHandler(Update, bl.gate)), (0, CommandHandler("on", hd.cmd_on)))
    app.bot_data["backlog"] = bl.Backlog()
    await app.process_update(tg_update(3, text="/on bed_ac", user=STRANGER, age_s=3600, bot=app.bot))
    (t,) = turns(tmp_path)
    assert (t["outcome"], t["user_id"], t["allowed"]) == ("dropped_offline", STRANGER, False)
    assert not ctx.sent


async def test_non_update_objects_pass_untracked(tmp_path):
    app = tracked_app(make_ctx(tmp_path))
    await app.process_update(SimpleNamespace())
    assert turns(tmp_path) == []


# ---- scheduler ------------------------------------------------------------------------

async def test_schedule_fire_is_a_scheduler_turn(env):  # noqa: F811
    env.sch.tracker = env.ctx.tracker
    await env.add(sched())
    await env.tick(FIRE + timedelta(seconds=3))
    (t,) = turns(env.ctx.settings.log_dir.parent)
    assert (t["surface"], t["intent"], t["outcome"], t["devices"]) == ("scheduler", "schedule:on", "ok", ["bed_ac"])
    assert t["request_id"].startswith("s7f3a@") and t["user_id"] == 0


# ---- review follow-ups ------------------------------------------------------------------

async def test_refusal_before_any_device_call_is_not_ok(tmp_path):
    ctx = make_ctx(tmp_path)
    async with ctx.tracker.turn(surface="slash"):
        await hd.cmd_on(make_update(ME), make_context(ctx, ["nosuch"]))
    assert turns(tmp_path)[0]["outcome"] == "device_not_found"


def test_button_intent_drops_ids():
    assert button_intent("kb:q:bed_ac:t+") == "kb:q"
    assert button_intent("sw:d1a2b3:time:2300") == "sw:time"
    assert button_intent("sw:-:newfor:bed_ac") == "sw:newfor"
    assert button_intent("ap:d1a2b3:223a0:t+") == "ap:t+"
    assert button_intent("ap:!bed_ac:223a0:done") == "card:done"
    assert button_intent("") is None


def test_lag_dropped_when_the_pi_clock_is_behind(tmp_path):
    ctx = make_ctx(tmp_path)
    assert turn_fields(tg_update(1, text="hi", age_s=-600), ctx, NOW.timestamp())["lag_s"] is None
    assert turn_fields(tg_update(1, text="hi", age_s=-2), ctx, NOW.timestamp())["lag_s"] == -2


async def test_long_telegram_text_keeps_the_cut_marker(tmp_path):
    ctx = make_ctx(tmp_path)
    async with ctx.tracker.turn(**turn_fields(tg_update(1, text="y" * 3000), ctx, NOW.timestamp())):
        pass
    assert turns(tmp_path)[0]["raw"].endswith("...")


async def test_failed_schedule_fire_keeps_the_device_code(env):  # noqa: F811
    from iotbot.devices.hub import HubError
    env.sch.tracker = env.ctx.tracker
    env.ctx.fail = HubError("no reply")
    await env.add(sched())
    await env.tick(FIRE + timedelta(seconds=3))
    (t,) = turns(env.ctx.settings.log_dir.parent)
    assert (t["outcome"], t["error"]) == ("failed", "send_failed")
    assert t["attempts"] == t["sends"] and t["attempts"] > 1
