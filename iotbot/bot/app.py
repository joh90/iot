"""Build the PTB Application."""

from __future__ import annotations

import logging

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from iotbot.bot import handlers as hd
from iotbot.context import AppContext, expected_devices

logger = logging.getLogger(__name__)

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
]


def build_application(ctx: AppContext) -> Application:
    async def post_init(app: Application) -> None:
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
        logger.info("%s running", ctx.settings.bot_name)

    async def post_stop(app: Application) -> None:
        # Before the bot shuts down, so a last fire can still send its notice
        if ctx.scheduler:
            await ctx.scheduler.stop()

    app = (
        Application.builder()
        .token(ctx.settings.bot_token)
        # Sends to one RM are serialized by the hub's per-device lock
        .concurrent_updates(8)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )
    app.bot_data["ctx"] = ctx
    register(app)
    return app


def register(app: Application) -> None:
    # New messages only: editing an old "/off ac" must not fire IR again
    new_only = filters.UpdateType.MESSAGE
    for name, func in (("ping", hd.cmd_ping), ("start", hd.cmd_start), ("status", hd.cmd_status),
                       ("list", hd.cmd_list), ("keyboard", hd.cmd_keyboard), ("on", hd.cmd_on),
                       ("off", hd.cmd_off), ("d", hd.cmd_device), ("user", hd.cmd_user),
                       ("adduser", hd.cmd_adduser), ("schedule", hd.cmd_schedule)):
        app.add_handler(CommandHandler(name, func, filters=new_only))
    # Plain text is only read as the answer to the wizard's "Type a time"
    app.add_handler(MessageHandler(new_only & filters.TEXT & ~filters.COMMAND, hd.on_text))
    app.add_handler(CallbackQueryHandler(hd.on_button))
    app.add_error_handler(hd.on_error)
