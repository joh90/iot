"""Schedules as data: what to send, to which device, and when.

One record per schedule in `schedules.json`. Weekly schedules repeat (`s` ids);
timers fire once and are then deleted (`t` ids). Actions are typed steps so a
schedule never turns on an AC that someone switched off by hand:

    on       full AC state (power on), sent as is
    off      the bot's last-known AC state, powered off (same as the Off button)
    adjust   change some settings of the last-known state; skipped if that is off
    capture  a captured code by feature key, for devices without the AC encoder

Parsing is strict and round-trips: `parse_schedule(s.to_dict()) == s`.
"""

from __future__ import annotations

import math
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from iotbot.ac.state import AcState, AcStateError
from iotbot.devices.model import ID_RE as DEVICE_ID_RE

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
ID_RE = re.compile(r"^[st][0-9a-f]{4}$")
FEATURE_RE = re.compile(r"^[a-z0-9_]{1,32}$")
ADJUSTABLE = ("temp", "fan", "vane", "powerful")
MAX_LABEL = 32
FILE_VERSION = 1


class ScheduleError(ValueError):
    pass


# ---- actions ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class On:
    state: AcState
    kind: Literal["on"] = "on"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "on", "state": self.state.to_dict()}


@dataclass(frozen=True, slots=True)
class Off:
    kind: Literal["off"] = "off"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "off"}


@dataclass(frozen=True, slots=True)
class Adjust:
    changes: tuple[tuple[str, Any], ...]   # sorted (setting, value) pairs
    kind: Literal["adjust"] = "adjust"

    def apply(self, state: AcState) -> AcState:
        return state.with_changes(**dict(self.changes))

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "adjust", "changes": dict(self.changes)}


@dataclass(frozen=True, slots=True)
class Capture:
    key: str
    kind: Literal["capture"] = "capture"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "capture", "key": self.key}


Action = On | Off | Adjust | Capture
AC_ACTIONS = (On, Off, Adjust)   # need a device managed by the AC encoder


def make_adjust(changes: Any) -> Adjust:
    if not isinstance(changes, dict) or not changes:
        raise ScheduleError("adjust needs at least one setting to change")
    unknown = set(changes) - set(ADJUSTABLE)
    if unknown:
        raise ScheduleError(f"adjust can change {', '.join(ADJUSTABLE)}, not {', '.join(sorted(map(str, unknown)))}")
    try:
        AcState(power=True, temp=24).with_changes(**changes)   # validates each value
    except AcStateError as e:
        raise ScheduleError(str(e)) from None
    return Adjust(tuple(sorted(changes.items())))


def parse_action(raw: Any) -> Action:
    if not isinstance(raw, dict):
        raise ScheduleError("action must be an object")
    kind = raw.get("kind")
    keys = set(raw) - {"kind"}
    try:
        if kind == "on" and keys == {"state"}:
            state = AcState.from_dict(raw["state"])
            if not state.power:
                raise ScheduleError("an 'on' action needs power true; use 'off' to switch off")
            return On(state)
        if kind == "off" and not keys:
            return Off()
        if kind == "adjust" and keys == {"changes"}:
            return make_adjust(raw["changes"])
        if kind == "capture" and keys == {"key"}:
            key = raw["key"]
            if not (isinstance(key, str) and FEATURE_RE.fullmatch(key)):
                raise ScheduleError(f"bad feature key {key!r}")
            return Capture(key)
    except AcStateError as e:
        raise ScheduleError(str(e)) from None
    raise ScheduleError(f"unknown action {raw!r}")


# ---- when ------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Weekly:
    time: str                    # "HH:MM", local time
    days: tuple[str, ...]        # subset of DAYS, in DAYS order
    kind: Literal["weekly"] = "weekly"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "weekly", "time": self.time, "days": list(self.days)}


@dataclass(frozen=True, slots=True)
class Once:
    at: datetime                 # timezone-aware
    kind: Literal["once"] = "once"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "once", "at": self.at.isoformat()}


When = Weekly | Once


def make_weekly(time: Any, days: Any) -> Weekly:
    if not (isinstance(time, str) and TIME_RE.fullmatch(time)):
        raise ScheduleError(f"time must be HH:MM (24h), got {time!r}")
    if not isinstance(days, (list, tuple)) or not days:
        raise ScheduleError("pick at least one day")
    bad = [d for d in days if d not in DAYS]
    if bad:
        raise ScheduleError(f"unknown day(s) {bad!r}; use {', '.join(DAYS)}")
    if len(set(days)) != len(days):
        raise ScheduleError("a day is listed twice")
    return Weekly(time, tuple(d for d in DAYS if d in days))


def parse_when(raw: Any) -> When:
    if not isinstance(raw, dict):
        raise ScheduleError("when must be an object")
    kind = raw.get("kind")
    if kind == "weekly" and set(raw) == {"kind", "time", "days"}:
        weekly = make_weekly(raw["time"], raw["days"])
        if list(weekly.days) != list(raw["days"]):
            raise ScheduleError("days must be in mon..sun order")
        return weekly
    if kind == "once" and set(raw) == {"kind", "at"}:
        at = parse_dt(raw["at"], "at")
        if at.microsecond:
            raise ScheduleError("timer time must be whole seconds")
        return Once(at)
    raise ScheduleError(f"unknown when {raw!r}")


def parse_dt(raw: Any, name: str) -> datetime:
    if not isinstance(raw, str):
        raise ScheduleError(f"{name} must be an ISO date-time string")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise ScheduleError(f"{name} {raw!r} is not an ISO date-time") from None
    if dt.tzinfo is None:
        raise ScheduleError(f"{name} {raw!r} has no timezone offset")
    return dt


# ---- schedule --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LastFired:
    at: datetime
    occurrence: str              # "<id>@<local fire time ISO>"
    result: str                  # started (sending now) | ok | failed | superseded | missed | skipped

    def to_dict(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "occurrence": self.occurrence, "result": self.result}


RESULTS = ("started", "ok", "failed", "superseded", "missed", "skipped")


@dataclass(frozen=True, slots=True)
class Schedule:
    id: str
    device: str
    action: Action
    when: When
    created_by: int
    created_at: datetime
    updated_by: int
    updated_at: datetime
    label: str = ""
    rev: int = 1
    enabled: bool = True
    paused_until: datetime | None = None
    skip_dates: tuple[date, ...] = ()          # local fire dates, sorted
    last_fired: LastFired | None = None
    only_if: None = field(default=None)        # reserved for presence conditions

    @property
    def is_timer(self) -> bool:
        return isinstance(self.when, Once)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "rev": self.rev, "label": self.label, "device": self.device,
            "action": self.action.to_dict(), "when": self.when.to_dict(),
            "enabled": self.enabled,
            "paused_until": self.paused_until.isoformat() if self.paused_until else None,
            "skip_dates": [d.isoformat() for d in self.skip_dates],
            "only_if": None,
            "created_by": self.created_by, "created_at": self.created_at.isoformat(),
            "updated_by": self.updated_by, "updated_at": self.updated_at.isoformat(),
            "last_fired": self.last_fired.to_dict() if self.last_fired else None,
        }


FIELDS = frozenset(("id", "rev", "label", "device", "action", "when", "enabled", "paused_until", "skip_dates",
                    "only_if", "created_by", "created_at", "updated_by", "updated_at", "last_fired"))


def clean_label(raw: Any) -> str:
    """Printable, single-spaced, at most MAX_LABEL characters."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(raw or ""))
    return " ".join(text.split())[:MAX_LABEL].strip()


def _user_id(raw: Any, name: str) -> int:
    # 0 is the system/scheduler actor
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ScheduleError(f"{name} must be a user id, got {raw!r}")
    return raw


def parse_schedule(raw: Any) -> Schedule:
    """A Schedule from its JSON form. Raises ScheduleError on anything unexpected."""
    if not isinstance(raw, dict):
        raise ScheduleError("schedule must be an object")
    if set(raw) != FIELDS:
        missing, extra = FIELDS - set(raw), set(raw) - FIELDS
        raise ScheduleError(f"fields missing {sorted(missing)} / unexpected {sorted(extra)}")
    sid = raw["id"]
    if not (isinstance(sid, str) and ID_RE.fullmatch(sid)):
        raise ScheduleError(f"bad id {sid!r}")
    rev = raw["rev"]
    if isinstance(rev, bool) or not isinstance(rev, int) or rev < 1:
        raise ScheduleError(f"rev must be a positive number, got {rev!r}")
    label = raw["label"]
    if not isinstance(label, str) or clean_label(label) != label:
        raise ScheduleError(f"bad label {label!r}")
    device = raw["device"]
    if not (isinstance(device, str) and DEVICE_ID_RE.fullmatch(device)):
        raise ScheduleError(f"bad device id {device!r}")
    when = parse_when(raw["when"])
    if (sid[0] == "t") != isinstance(when, Once):
        raise ScheduleError(f"id {sid} does not match a {when.kind} schedule (s = weekly, t = once)")
    if isinstance(when, Once) and (raw["skip_dates"] or raw["paused_until"] is not None):
        raise ScheduleError("a timer cannot have skip dates or a pause; disable or delete it instead")
    if not isinstance(raw["enabled"], bool):
        raise ScheduleError("enabled must be true or false")
    if raw["only_if"] is not None:
        raise ScheduleError("only_if is reserved and must be null")
    skips = raw["skip_dates"]
    if not isinstance(skips, list):
        raise ScheduleError("skip_dates must be a list")
    try:
        skip_dates = tuple(date.fromisoformat(d) for d in skips)
    except (TypeError, ValueError):
        raise ScheduleError(f"bad skip_dates {skips!r}") from None
    if list(skip_dates) != sorted(set(skip_dates)):
        raise ScheduleError("skip_dates must be sorted with no repeats")
    lf = raw["last_fired"]
    last_fired = None
    if lf is not None:
        if not (isinstance(lf, dict) and set(lf) == {"at", "occurrence", "result"}
                and isinstance(lf["occurrence"], str) and lf["result"] in RESULTS):
            raise ScheduleError(f"bad last_fired {lf!r}")
        last_fired = LastFired(parse_dt(lf["at"], "last_fired.at"), lf["occurrence"], lf["result"])
    return Schedule(
        id=sid, rev=rev, label=label, device=device, action=parse_action(raw["action"]), when=when,
        enabled=raw["enabled"],
        paused_until=None if raw["paused_until"] is None else parse_dt(raw["paused_until"], "paused_until"),
        skip_dates=skip_dates, last_fired=last_fired,
        created_by=_user_id(raw["created_by"], "created_by"), created_at=parse_dt(raw["created_at"], "created_at"),
        updated_by=_user_id(raw["updated_by"], "updated_by"), updated_at=parse_dt(raw["updated_at"], "updated_at"),
    )


def new_id(timer: bool, taken: set[str]) -> str:
    """A short unused id: `s` + 4 hex for weekly, `t` + 4 hex for timers.
    Pass `ScheduleStore.ids()`, which includes broken entries."""
    prefix = "t" if timer else "s"
    for _ in range(1000):
        sid = prefix + secrets.token_hex(2)
        if sid not in taken:
            return sid
    raise ScheduleError("could not find a free schedule id")   # 65536 ids; limits keep us far below


def validate_file(data: Any) -> None:
    """Top-level shape only. Bad individual schedules load as broken (see ScheduleStore).

    `saved_at` (epoch seconds of the last save) feeds the boot clock check; optional so
    a hand-written file still loads."""
    if not isinstance(data, dict) or not {"version", "schedules"} <= set(data) <= {"version", "schedules", "saved_at"}:
        raise TypeError('schedules.json must be {"version": 1, "schedules": {...}}')
    saved = data.get("saved_at")
    if saved is not None and (isinstance(saved, bool) or not isinstance(saved, (int, float))
                              or not math.isfinite(saved)):
        raise TypeError(f"schedules.json: saved_at must be a number, got {saved!r}")
    if type(data["version"]) is not int or data["version"] != FILE_VERSION:
        raise ValueError(f"schedules.json version {data['version']!r} is not supported (expected {FILE_VERSION})")
    if not isinstance(data["schedules"], dict):
        raise TypeError("schedules.json: 'schedules' must be an object of {id: schedule}")


def empty_file() -> dict[str, Any]:
    return {"version": FILE_VERSION, "schedules": {}}
