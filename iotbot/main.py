"""Entry point: `uv run iotbot` (reads .env from the working directory)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from iotbot.config import ConfigError, load_settings
from iotbot.store import StoreError


class RedactFilter(logging.Filter):
    """Replace the bot token's secret part (also inside URLs and tracebacks) with '<token>'."""

    def __init__(self, token: str):
        super().__init__()
        secret = token.split(":", 1)[-1]
        self.secret = secret if len(secret) >= 20 else ""   # real secrets are 35 chars; never scrub "x"

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secret:
            return True
        msg = record.getMessage()
        if self.secret in msg:
            record.msg, record.args = msg.replace(self.secret, "<token>"), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text and self.secret in record.exc_text:
            record.exc_text = record.exc_text.replace(self.secret, "<token>")
        if record.stack_info and self.secret in record.stack_info:
            record.stack_info = record.stack_info.replace(self.secret, "<token>")
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="iotbot", description="Telegram IR/Broadlink home bot")
    parser.add_argument("--env-file", default=".env", help="path to the .env file (default: ./.env)")
    parser.add_argument("--check", action="store_true",
                        help="load config and data files, print warnings, and exit without connecting")
    parser.add_argument("--discover", action="store_true",
                        help="find and authenticate every Broadlink device, print the result, and exit "
                             "(never contacts Telegram or sends IR; BOT_TOKEN may be empty)")
    args = parser.parse_args(argv)

    env_file = Path(args.env_file)
    try:
        settings = load_settings(env_file=env_file.name, base_dir=env_file.resolve().parent,
                                 require_token=not args.discover)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=getattr(logging, settings.log_level, logging.INFO),
    )
    # httpx logs every Telegram poll at INFO, which includes the bot token in the URL
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # PTB puts the full token in some errors (e.g. a rejected token, logged by its retry loop
    # before main() can catch it), so scrub it from every record any handler writes
    if settings.bot_token:
        for handler in logging.getLogger().handlers:
            handler.addFilter(RedactFilter(settings.bot_token))

    from iotbot.bot.app import build_application
    from iotbot.context import build_context

    try:
        ctx = build_context(settings)
    except (StoreError, OSError, ValueError, TypeError) as e:
        logging.getLogger(__name__).critical("Refusing to start: %s", e)
        return 3

    if args.check:
        # Warnings were already logged by build_context
        print(f"rooms={len(ctx.registry.rooms)} devices={len(ctx.registry.devices)} "
              f"users={len(ctx.users.list())} warnings={len(ctx.warnings)}")
        return 0

    if args.discover:
        return _discover(ctx)

    from telegram.error import InvalidToken

    app = build_application(ctx)
    try:
        # Keep retrying the first connection: at boot DNS may come up after network-online
        app.run_polling(drop_pending_updates=False, bootstrap_retries=-1)
    except InvalidToken:
        # Never log the exception text: PTB puts the full token in it (RedactFilter is the backstop)
        logging.getLogger(__name__).critical("BOT_TOKEN was rejected by Telegram; check .env")
        return 2
    return 0


def _discover(ctx) -> int:
    """Exit 0 if every expected device answered, 1 otherwise."""
    import asyncio

    from iotbot.context import expected_devices

    asyncio.run(ctx.hub.start(expected_devices(ctx.registry)))
    statuses = list(ctx.hub.status.values())
    for st in statuses:
        state = f"online {st.type} {st.ip}" if st.online else f"OFFLINE ({st.error})"
        print(f"{st.label}: {st.mac} {state}")
    online = sum(st.online for st in statuses)
    print(f"{online}/{len(statuses)} devices online")
    return 0 if statuses and online == len(statuses) else 1


if __name__ == "__main__":
    sys.exit(main())
