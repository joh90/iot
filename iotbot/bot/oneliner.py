"""`/schedule add ...`: one line for people who know what they want.

    /schedule add bedroom on 23:00 sun-thu cool 22 fan 3
    /schedule add bed_ac off 7am weekdays name=Wake
    /schedule add office set 25 14:00 daily
    /schedule add bedroom off in 2h
    /schedule add tv power_off 01:00 tomorrow

Parsed into the same draft the wizard builds, then shown as the same preview card.
"""

from __future__ import annotations

import re
import shlex
from datetime import timedelta
from typing import Any

from iotbot.ac.state import AcState, AcStateError
from iotbot.bot.wizard import Draft, Wizard, parse_time
from iotbot.schedule import timing as tm
from iotbot.schedule.model import DAYS

USAGE = ("Usage: /schedule add <device or room> <on|off|set TEMP|button> <time> [days] [settings] "
         "[name=Label]\n"
         "Examples:\n"
         "/schedule add bedroom on 23:00 sun-thu cool 22 fan 3\n"
         "/schedule add bedroom off in 2h\n"
         "/schedule add office set 25 14:00 daily\n"
         "Days: mon,wed or sun-thu, weekdays, weekends, daily; or once, today, tomorrow.\n"
         "Settings: 16-31 (temp), fan auto|1-4|quiet, vane auto|1-5|swing, powerful.\n"
         'A name with spaces needs quotes: name="Bed time".')

DAY_ALIASES = {"weekdays": DAYS[:5], "weekday": DAYS[:5], "weekends": ("sat", "sun"), "weekend": ("sat", "sun"),
               "daily": DAYS, "everyday": DAYS, "every-day": DAYS}
DAY_NAMES = {d: d for d in DAYS} | {"mon": "mon", "tues": "tue", "wed": "wed", "thur": "thu", "thurs": "thu",
                                    "sat": "sat", "sun": "sun"} | {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri",
    "saturday": "sat", "sunday": "sun"}
IN_RE = re.compile(r"^(\d{1,3})(m|min|mins|h|hr|hrs|hour|hours)$")


class LineError(ValueError):
    pass


def parse_days(word: str) -> list[str] | None:
    """'sun-thu', 'mon,wed,fri', 'weekdays', 'daily', 'fri' -> days in week order (None if not days)."""
    w = word.lower()
    if w in DAY_ALIASES:
        return list(DAY_ALIASES[w])
    days: set[str] = set()
    for part in w.split(","):
        if "-" in part:
            a, _, b = part.partition("-")
            if a not in DAY_NAMES or b not in DAY_NAMES:
                return None
            i, j = DAYS.index(DAY_NAMES[a]), DAYS.index(DAY_NAMES[b])
            days |= {DAYS[(i + k) % 7] for k in range((j - i) % 7 + 1)}
        elif part in DAY_NAMES:
            days.add(DAY_NAMES[part])
        else:
            return None
    return [d for d in DAYS if d in days] or None


def resolve_device(w: Wizard, word: str, action: str) -> str:
    """A device id, or a room with exactly one device that can do `action`."""
    reg = w.ctx.registry
    by_lower = {d.lower(): d for d in w.schedulable()}
    if word.lower() in by_lower:
        return by_lower[word.lower()]
    room = reg.rooms.get(word)
    if room is None:
        lower = {k.lower(): k for k in reg.rooms}
        room = reg.rooms.get(lower.get(word.lower(), ""))
    if room is None:
        raise LineError(f"No device or room '{word}'. /list shows them.")
    def can(d: str) -> bool:
        feats = w.ctx.devices.get(d).features
        if action == "set":
            return w.managed(d)
        if action == "on":
            return w.managed(d) or "power_on" in feats
        if action == "off":
            return w.can_timer(d)
        return action in feats

    fits = [d for d in room.devices if d in w.schedulable() and can(d)]
    managed = [d for d in fits if w.managed(d)]
    if len(managed) == 1 and action in ("on", "off", "set"):
        return managed[0]
    if len(fits) == 1:
        return fits[0]
    if not fits:
        raise LineError(f"Nothing in room '{room.name}' can do '{action}'.")
    raise LineError(f"Room '{room.name}' has several devices that can; name one: {', '.join(fits)}.")


def parse_settings(words: list[str], base: AcState) -> AcState:
    st = base
    i = 0
    try:
        while i < len(words):
            w = words[i].lower()
            nxt = words[i + 1].lower() if i + 1 < len(words) else ""
            if w in ("cool", "c"):
                pass
            elif re.fullmatch(r"\d{2}c?", w):
                st = st.with_changes(temp=int(w.rstrip("c")))
            elif w == "fan" and nxt:
                st = st.with_changes(fan=int(nxt) if nxt.isdigit() else nxt)
                i += 1
            elif w == "quiet":
                st = st.with_changes(fan="quiet")
            elif w == "vane" and nxt:
                st = st.with_changes(vane=int(nxt) if nxt.isdigit() else nxt)
                i += 1
            elif w == "swing":
                st = st.with_changes(vane="swing")
            elif w == "powerful":
                st = st.with_changes(powerful=True)
            else:
                raise LineError(f"Do not understand '{words[i]}'.")
            i += 1
    except AcStateError as e:
        raise LineError(str(e)) from None
    return st


def parse_add(w: Wizard, user_id: int, text: str) -> Draft:
    """Parse the words after `/schedule add` into a draft at the preview step."""
    try:
        words = shlex.split(text)
    except ValueError:
        words = text.split()        # e.g. an apostrophe in a name: no quoting then
    label = ""
    rest = []
    for word in words:
        if word.lower().startswith(("name=", "label=")):
            label = word.split("=", 1)[1]
        else:
            rest.append(word)
    if len(rest) < 3:
        raise LineError(USAGE)
    dev_word, verb = rest[0], rest[1].lower()
    rest = rest[2:]
    action_word = "set" if verb in ("set", "temp", "adjust") else verb
    device = resolve_device(w, dev_word, action_word if action_word in ("on", "off", "set") else verb)

    action: dict[str, Any] | None = None
    if verb in ("set", "temp", "adjust"):
        if not w.managed(device):
            raise LineError(f"{device} cannot change temperature on its own; use on or off.")
        if not rest or not re.fullmatch(r"\d{2}c?", rest[0].lower()):
            raise LineError("set needs a temperature, e.g. set 25.")
        action = {"kind": "adjust", "changes": {"temp": int(rest[0].lower().rstrip("c"))}}
        rest = rest[1:]
    elif verb == "off":
        action = {"kind": "off"}
    elif verb == "on" and w.managed(device):
        action = None    # needs the settings after the time
    else:
        dev = w.ctx.devices.get(device)
        key = {"on": "power_on"}.get(verb, verb)
        if key not in dev.features:
            raise LineError(f"{device} has no '{verb}'. Its buttons: {', '.join(dev.features)}.")
        action = {"kind": "capture", "key": key}

    # When: "in 2h" | time [days|once|today|tomorrow]
    now = w.now()
    kind, hhmm, days, date_ = None, None, [], None
    if rest and rest[0].lower() == "in" and len(rest) > 1 and (m := IN_RE.match(rest[1].lower())):
        n, unit = int(m.group(1)), m.group(2)
        at = now + timedelta(minutes=n * (60 if unit.startswith("h") else 1))
        kind, hhmm, date_ = "once", f"{at:%H:%M}", at.date()
        rest = rest[2:]
    elif rest and (t := parse_time(rest[0])):
        hhmm = t
        rest = rest[1:]
        if rest and rest[0].lower() in ("once", "today", "tonight", "tomorrow"):
            kind = "once"
            word = rest[0].lower()
            if word == "tomorrow":
                date_ = now.date() + timedelta(days=1)
            elif word in ("today", "tonight"):
                date_ = now.date()
                if tm.local_time(date_, hhmm, w.svc.tz) <= now:
                    raise LineError(f"{hhmm} has already passed today; say tomorrow, or once for the next {hhmm}.")
            rest = rest[1:]
        elif rest and (ds := parse_days(rest[0])):
            kind, days = "weekly", ds
            rest = rest[1:]
        else:
            kind, days = "weekly", list(DAYS)
    else:
        raise LineError("Give a time (23:00, 11pm) or 'in 2h'.\n" + USAGE)

    if action is None:
        action = {"kind": "on", "state": parse_settings(rest, w.preset(device)).to_dict()}
    elif rest:
        raise LineError(f"Do not understand '{' '.join(rest)}' (settings only go with on).")

    return w.new_draft(user_id, device=device, action=action, kind=kind, time=hhmm, days=days,
                       label=label, step="prev", date=date_)
