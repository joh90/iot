"""Build the PTB Application."""

from __future__ import annotations

import logging

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, filters

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
]


def build_application(ctx: AppContext) -> Application:
    async def post_init(app: Application) -> None:
        await ctx.hub.start(expected_devices(ctx.registry))
        try:
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        except Exception as e:  # noqa: BLE001 -- cosmetic, never block startup
            logger.warning("Could not set the command menu: %s", e)
        logger.info("%s running", ctx.settings.bot_name)

    app = (
        Application.builder()
        .token(ctx.settings.bot_token)
        # Sends to one RM are serialized by the hub's per-device lock
        .concurrent_updates(8)
        .post_init(post_init)
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
                       ("adduser", hd.cmd_adduser)):
        app.add_handler(CommandHandler(name, func, filters=new_only))
    app.add_handler(CallbackQueryHandler(hd.on_button))
    app.add_error_handler(hd.on_error)
