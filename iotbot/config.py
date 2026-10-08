"""Settings loaded from the environment (and `.env` if present)."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    bot_name: str
    devices_path: Path
    commands_path: Path
    users_path: Path
    state_dir: Path
    log_dir: Path
    timezone: str
    discover_timeout: float
    log_level: str


def _path(env: dict[str, str], key: str, default: str, base: Path) -> Path:
    p = Path(env.get(key) or default).expanduser()
    return p if p.is_absolute() else base / p


def load_settings(env_file: str | os.PathLike | None = ".env",
                  environ: dict[str, str] | None = None,
                  base_dir: Path | None = None) -> Settings:
    """Build Settings without touching os.environ.

    `env_file` and relative data paths resolve against `base_dir` (default: cwd).
    Real environment variables win over `.env` values. Passing `environ` skips both.
    """
    base = base_dir or Path.cwd()
    env_path = None
    if environ is None:
        file_values: dict[str, str] = {}
        if env_file is not None:
            env_path = Path(env_file)
            if not env_path.is_absolute():
                env_path = base / env_path
            if env_path.exists():
                file_values = {k: v for k, v in dotenv_values(env_path).items() if v is not None}
        # Real env wins, but an empty real value never hides a .env value
        environ = {**file_values, **{k: v for k, v in os.environ.items() if v != ""}}

    token = (environ.get("BOT_TOKEN") or "").strip()
    if not token or ":" not in token:
        where = f" (looked in {env_path} and the environment)" if env_path else ""
        raise ConfigError(f"BOT_TOKEN is missing or malformed, expected '<id>:<secret>'{where}; see .env.example")

    try:
        discover_timeout = float(environ.get("DISCOVER_TIMEOUT") or 5)
    except ValueError as e:
        raise ConfigError(f"DISCOVER_TIMEOUT must be a number: {e}") from None
    if not math.isfinite(discover_timeout) or discover_timeout <= 0:
        raise ConfigError("DISCOVER_TIMEOUT must be a positive number of seconds")

    return Settings(
        bot_token=token,
        bot_name=environ.get("BOT_NAME") or "Home IoT",
        devices_path=_path(environ, "DEVICES_PATH", "devices.json", base),
        commands_path=_path(environ, "COMMANDS_PATH", "commands.json", base),
        users_path=_path(environ, "USERS_PATH", "users.json", base),
        state_dir=_path(environ, "STATE_DIR", "state", base),
        log_dir=_path(environ, "LOG_DIR", "logs", base),
        timezone=environ.get("TZ_NAME") or "Asia/Singapore",
        discover_timeout=discover_timeout,
        log_level=(environ.get("LOG_LEVEL") or "INFO").upper(),
    )
