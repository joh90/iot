"""Settings loaded from the environment (and `.env` if present)."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    # False (AC_ENCODER=off): ACs send their captured codes, as v1 did
    ac_encoder: bool = True
    # The PC llama-server through the SSH tunnel (README "Tunnel to the LLM PC")
    llm_url: str = "http://127.0.0.1:8001"
    # Phase 4 chat assistant. Off: free text gets a one-line reply (the wizard still works)
    llm_enabled: bool = True
    # llama-server slot the bot pins to (slot 0 is left for the owner); -1 = let the server pick
    llm_slot: int = 1
    # Home for outside conditions: one of NEA's 47 forecast areas, and the air regions to show
    outside_area: str = "City"
    outside_regions: tuple[str, ...] = ("central",)
    # Who may approve and remove users; empty = every approved user may (all equal)
    admin_ids: frozenset[int] = frozenset()
    # Phase 8 presence: probe paired phones on the home Wi-Fi (off: /phone says so, nothing probed)
    presence: bool = True
    # The home Wi-Fi to look for phones on, e.g. 192.168.1.0/24; empty = the Pi's own /24
    presence_subnet: str = ""


def _path(env: dict[str, str], key: str, default: str, base: Path) -> Path:
    p = Path(env.get(key) or default).expanduser()
    return p if p.is_absolute() else base / p


def load_settings(env_file: str | os.PathLike | None = ".env",
                  environ: dict[str, str] | None = None,
                  base_dir: Path | None = None,
                  require_token: bool = True) -> Settings:
    """Build Settings without touching os.environ.

    `require_token=False` allows an empty BOT_TOKEN (discovery-only runs that never reach Telegram).

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
    if (token or require_token) and ":" not in token:
        where = f" (looked in {env_path} and the environment)" if env_path else ""
        raise ConfigError(f"BOT_TOKEN is missing or malformed, expected '<id>:<secret>'{where}; see .env.example")

    try:
        discover_timeout = float(environ.get("DISCOVER_TIMEOUT") or 5)
    except ValueError as e:
        raise ConfigError(f"DISCOVER_TIMEOUT must be a number: {e}") from None
    if not math.isfinite(discover_timeout) or discover_timeout <= 0:
        raise ConfigError("DISCOVER_TIMEOUT must be a positive number of seconds")

    ac_raw = (environ.get("AC_ENCODER") or "on").strip().lower()
    if ac_raw not in ("on", "off"):
        raise ConfigError(f"AC_ENCODER must be 'on' or 'off', got '{ac_raw}'")

    llm_raw = (environ.get("LLM_ENABLED") or "on").strip().lower()
    if llm_raw not in ("on", "off"):
        raise ConfigError(f"LLM_ENABLED must be 'on' or 'off', got '{llm_raw}'")
    try:
        llm_slot = int(environ.get("LLM_SLOT") or 1)
    except ValueError:
        raise ConfigError("LLM_SLOT must be a whole number (-1 = any slot)") from None
    if llm_slot < -1:
        raise ConfigError("LLM_SLOT must be -1 or more")

    llm_url = (environ.get("LLM_URL") or "http://127.0.0.1:8001").strip().rstrip("/")
    if not llm_url.startswith(("http://", "https://")):
        raise ConfigError(f"LLM_URL must start with http:// or https://, got '{llm_url}'")

    from iotbot.outside.weather import AREAS, REGIONS
    area_raw = " ".join((environ.get("OUTSIDE_AREA") or "City").split())
    outside_area = next((a for a in AREAS if a.lower() == area_raw.lower()), None)
    if outside_area is None:
        raise ConfigError(f"OUTSIDE_AREA '{area_raw}' is not one of NEA's areas (e.g. Bedok, City, Toa Payoh)")
    outside_regions = tuple(dict.fromkeys(r.strip().lower() for r in (environ.get("OUTSIDE_REGIONS") or "central")
                                          .split(",") if r.strip()))
    if not outside_regions or any(r not in REGIONS for r in outside_regions):
        raise ConfigError(f"OUTSIDE_REGIONS must be a comma list of {', '.join(REGIONS)}")

    admin_raw = [a.strip() for a in (environ.get("ADMIN_IDS") or "").split(",") if a.strip()]
    if any(not (a.isascii() and a.isdigit() and a[0] != "0") for a in admin_raw):
        raise ConfigError("ADMIN_IDS must be a comma list of Telegram user ids (numbers), or empty")
    admin_ids = frozenset(int(a) for a in admin_raw)

    presence_raw = (environ.get("PRESENCE") or "on").strip().lower()
    if presence_raw not in ("on", "off"):
        raise ConfigError(f"PRESENCE must be 'on' or 'off', got '{presence_raw}'")
    presence_subnet = (environ.get("PRESENCE_SUBNET") or "").strip()
    if presence_subnet:
        import ipaddress
        try:
            net = ipaddress.ip_network(presence_subnet, strict=False)
        except ValueError:
            raise ConfigError(f"PRESENCE_SUBNET '{presence_subnet}' is not a network like 192.168.1.0/24") from None
        if net.version != 4 or not net.is_private or not 22 <= net.prefixlen <= 30:
            raise ConfigError("PRESENCE_SUBNET must be a private IPv4 network from /22 to /30")
        presence_subnet = str(net)

    tz = environ.get("TZ_NAME") or "Asia/Singapore"
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"TZ_NAME '{tz}' is not a known timezone (e.g. Asia/Singapore)") from None

    return Settings(
        bot_token=token,
        bot_name=environ.get("BOT_NAME") or "Home IoT",
        devices_path=_path(environ, "DEVICES_PATH", "devices.json", base),
        commands_path=_path(environ, "COMMANDS_PATH", "commands.json", base),
        users_path=_path(environ, "USERS_PATH", "users.json", base),
        state_dir=_path(environ, "STATE_DIR", "state", base),
        log_dir=_path(environ, "LOG_DIR", "logs", base),
        timezone=tz,
        discover_timeout=discover_timeout,
        log_level=(environ.get("LOG_LEVEL") or "INFO").upper(),
        ac_encoder=ac_raw == "on",
        llm_url=llm_url,
        llm_enabled=llm_raw == "on",
        llm_slot=llm_slot,
        outside_area=outside_area,
        outside_regions=outside_regions,
        admin_ids=admin_ids,
        presence=presence_raw == "on",
        presence_subnet=presence_subnet,
    )
