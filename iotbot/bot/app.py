"""Build the PTB Application."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from iotbot.bot import handlers as hd
from iotbot.bot.backlog import Backlog, gate
from iotbot.context import AppContext, expected_devices

logger = logging.getLogger(__name__)

MAX_CLOCK_SKEW_S = 5

COMMANDS = [
    ("start", "Help and command list"),
    ("ping", "Your user id"),
    ("status", "Server, Broadlink devices, users"),
    ("list", "Rooms, devices and actions"),
    ("keyboard", "Button remote"),
    ("on", "Turn a device on"),
    ("off", "Turn a device off"),
    ("d", "Run a device action"),
    ("user", "Approved users"),
    ("adduser", "Approve a user"),
    ("schedule", "Schedules and timers"),
    ("stats", "Speed, failures, LLM health"),
    ("memory", "What the assistant remembers"),
    ("outside", "Weather and air (NEA)"),
]


def button_intent(data: str | None) -> str | None:
    """Group button taps by what they do, not by the draft or picker id inside them."""
    parts = (data or "").split(":")
    if not parts[0]:
        return None
    if parts[0] == "sw":                     # sw:<draft or ->:<action>:...
        return f"sw:{parts[2]}" if len(parts) > 2 else "sw"
    if parts[0] == "ap":                     # ap:<owner>:<state>:<op>; owner "!<device>" = Settings card
        card = "card" if len(parts) > 1 and parts[1].startswith("!") else "ap"
        return f"{card}:{parts[-1]}" if len(parts) > 3 else card
    return ":".join(parts[:2])


def turn_fields(update: Update, ctx: AppContext, now: float | None = None) -> dict[str, Any]:
    """What is known about a turn before any handler runs."""
    user = update.effective_user
    allowed = user is not None and ctx.users.is_allowed(user.id)
    name = (ctx.users.name_of(user.id) if allowed else user.full_name) if user else None
    msg, query = update.message, update.callback_query
    raw = intent = lag = None
    if query is not None:
        surface, raw, intent = "button", query.data, button_intent(query.data)
    elif msg is not None:
        raw = msg.text or msg.caption
        if raw and raw.startswith("/"):
            surface = "slash"
            intent = raw.split()[0].split("@")[0]
        else:
            surface, intent = "text", "text"
        if msg.date is not None:
            lag = round((now if now is not None else time.time()) - msg.date.timestamp())
            if lag < -MAX_CLOCK_SKEW_S:
                lag = None                   # Pi clock behind (no RTC, before NTP): meaningless
    else:
        surface = "other"
    return {
        "surface": surface,
        "user_id": user.id if user else None,
        "name": name,
        "allowed": allowed,
        "raw": raw,
        "intent": intent,
        "update_id": update.update_id,
        "request_id": f"tg:{update.update_id}",
        "lag_s": lag,
    }


class TrackedApplication(Application):
    """Writes one `turns` line per update: start to end, crashes and dropped updates included.
    Handlers must stay blocking (the default): a `block=False` handler would finish after the
    line is written and its notes would be lost."""

    async def process_update(self, update: object) -> None:
        ctx: AppContext | None = self.bot_data.get("ctx")
        if ctx is None or ctx.tracker is None or not isinstance(update, Update):
            await super().process_update(update)
            return
        try:
            fields = turn_fields(update, ctx)
        except Exception:  # noqa: BLE001 -- telemetry never stops an update
            logger.exception("Could not read turn fields")
            fields = {"surface": "other", "update_id": update.update_id}
        async with ctx.tracker.turn(**fields):
            await super().process_update(update)


def build_application(ctx: AppContext) -> Application:
    async def post_init(app: Application) -> None:
        # First, before polling starts: note what was queued while the bot was offline
        await app.bot_data["backlog"].load(app.bot)
        await ctx.hub.start(expected_devices(ctx.registry))
        try:
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        except Exception as e:  # noqa: BLE001 -- cosmetic, never block startup
            logger.warning("Could not set the command menu: %s", e)
        if ctx.notifier and ctx.scheduler:
            async def send(n):
                await hd.send_notice(app.bot, n)

            ctx.notifier.send = send
            ctx.scheduler.start()
            await ctx.notifier.report_problems()
        if ctx.holidays and ctx.assistant:
            # MOM's list: refreshed in the background when over a month old (never blocks startup)
            app.bot_data["holidays_task"] = asyncio.create_task(ctx.holidays.run())
        logger.info("%s running", ctx.settings.bot_name)

    async def post_stop(app: Application) -> None:
        # Before the bot shuts down, so a last fire can still send its notice
        if ctx.scheduler:
            await ctx.scheduler.stop()
        if ctx.assistant:
            await ctx.assistant.drain()
            await ctx.assistant.llm.aclose()
        if ctx.outside:
            await ctx.outside.nea.aclose()
        task = app.bot_data.get("holidays_task")
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if ctx.holidays:
            await ctx.holidays.aclose()

    app = (
        Application.builder()
        .application_class(TrackedApplication)
        .token(ctx.settings.bot_token)
        # Sends to one RM are serialized by the hub's per-device lock
        .concurrent_updates(8)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )
    app.bot_data["ctx"] = ctx
    app.bot_data["backlog"] = Backlog()
    register(app)
    return app


def register(app: Application) -> None:
    # Before everything else: drop taps and commands queued while the bot was offline
    app.add_handler(TypeHandler(Update, gate), group=-1)
    # New messages only: editing an old "/off ac" must not fire IR again
    new_only = filters.UpdateType.MESSAGE
    for name, func in (("ping", hd.cmd_ping), ("start", hd.cmd_start), ("status", hd.cmd_status),
                       ("list", hd.cmd_list), ("keyboard", hd.cmd_keyboard), ("on", hd.cmd_on),
                       ("off", hd.cmd_off), ("d", hd.cmd_device), ("user", hd.cmd_user),
                       ("adduser", hd.cmd_adduser), ("schedule", hd.cmd_schedule),
                       ("stats", hd.cmd_stats), ("memory", hd.cmd_memory), ("outside", hd.cmd_outside)):
        app.add_handler(CommandHandler(name, func, filters=new_only))
    # Plain text: the wizard's "Type a time", else the chat assistant (private chats only)
    app.add_handler(MessageHandler(new_only & filters.TEXT & ~filters.COMMAND, hd.on_text))
    app.add_handler(CallbackQueryHandler(hd.on_button))
    app.add_error_handler(hd.on_error)
