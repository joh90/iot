"""Entry point: `uv run iotbot` (reads .env from the working directory)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from iotbot.config import ConfigError, load_settings
from iotbot.store import StoreError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="iotbot", description="Telegram IR/Broadlink home bot")
    parser.add_argument("--env-file", default=".env", help="path to the .env file (default: ./.env)")
    parser.add_argument("--check", action="store_true",
                        help="load config and data files, print warnings, and exit without connecting")
    args = parser.parse_args(argv)

    env_file = Path(args.env_file)
    try:
        settings = load_settings(env_file=env_file.name, base_dir=env_file.resolve().parent)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=getattr(logging, settings.log_level, logging.INFO),
    )
    # httpx logs every Telegram poll at INFO, which includes the bot token in the URL
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from iotbot.bot.app import build_application
    from iotbot.context import build_context

    try:
        ctx = build_context(settings)
    except (StoreError, OSError, ValueError, TypeError) as e:
        logging.getLogger(__name__).critical("Refusing to start: %s", e)
        return 3

    if args.check:
        print(f"rooms={len(ctx.registry.rooms)} devices={len(ctx.registry.devices)} "
              f"users={len(ctx.users.list())} warnings={len(ctx.warnings)}")
        for w in ctx.warnings:
            print(f"warning: {w}")
        return 0

    app = build_application(ctx)
    app.run_polling(drop_pending_updates=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
