"""Diary: what happened at home, for questions like "who turned on the bedroom aircon last
night?" or "when did I last change the office timer?" (Phase 4.7).

Searches the logs the bot already keeps: device sends (everyone's: actions are shared),
schedule changes, and the ASKING person's own chat messages (never anyone else's). At most
MAX_LINES lines, newest first. Old messages that mention a forgotten note are hidden (best
effort: memory keeps hashed word pairs of forgotten notes, not their text).
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from iotbot.ac.state import AcState, AcStateError
from iotbot.llm.memory import Memory, mentions_forgotten
from iotbot.llm.tools import Tool
from iotbot.result import Actor
from iotbot.store import read_since

if TYPE_CHECKING:
    from iotbot.context import AppContext

MAX_LINES = 20
MAX_DAYS = 30
MAX_SAID = 150
DEVICE_KEYS = ("ts", "device", "feature", "user_id", "surface", "ok", "error", "maybe_delivered", "ac_state")
SCHEDULE_KEYS = ("ts", "event", "sid", "name", "user_id", "surface", "before", "after", "note")
TURN_KEYS = ("ts", "user_id", "surface", "raw", "intent")


class DiaryTools:
    def __init__(self, ctx: AppContext, memory: Memory | None):
        self.ctx = ctx
        self.memory = memory

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.ctx.settings.timezone)

    def tools(self, actor: Actor) -> list[Tool]:
        acs = [d for d in self.ctx.registry.devices if self.ctx.ac and self.ctx.ac.manages(d)]
        props: dict[str, Any] = {
            "what": {"type": "string", "enum": ["all", "devices", "schedules", "my_messages"],
                     "description": "devices = what was sent to aircons and by whom; my_messages = what "
                                    "this person typed to you before"},
            "days": {"type": "integer", "minimum": 1, "maximum": MAX_DAYS, "description": "how far back (7)"},
            "words": {"type": "string", "maxLength": 60,
                      "description": "only lines with all these whole words; prefer the device filter. "
                                     "Lines look like: 'bedroom-aircon: set ON cool 23C ..., by Alice via "
                                     "chat: ok' or 'you said: \"...\"'"},
        }
        if acs:
            props["device"] = {"type": "string", "enum": acs}
        return [Tool("diary", f"Search what happened at home: aircon changes and who made them, schedule "
                              f"changes, and this person's own earlier messages. Newest first, at most "
                              f"{MAX_LINES} lines.",
                     {"type": "object", "properties": props}, run=lambda a: self._search(actor, a))]

    async def _search(self, actor: Actor, args: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._search_sync, actor, args)

    def _search_sync(self, actor: Actor, args: dict[str, Any]) -> dict[str, Any]:
        what = args.get("what", "all")
        days = args.get("days", 7)
        device = args.get("device")
        words = set(re.findall(r"[a-z0-9]+", (args.get("words") or "").lower()))
        now = datetime.now(self.tz)
        since = now - timedelta(days=days)
        rows: list[tuple[datetime, str]] = []
        if what in ("all", "devices"):
            for e in read_since(self.ctx.devices.events, since, now, DEVICE_KEYS):
                if device and e.get("device") != device:
                    continue
                rows.append((_at(e), self._device_line(e)))
        if what in ("all", "schedules") and self.ctx.schedules is not None:
            for e in read_since(self.ctx.schedules.events, since, now, SCHEDULE_KEYS):
                if device and device not in (_dev(e.get("before")), _dev(e.get("after"))):
                    continue
                rows.append((_at(e), self._schedule_line(e)))
        if what in ("all", "my_messages") and self.ctx.tracker is not None and not device:
            forgotten = self.memory.forgotten() if self.memory else []
            # Only this person's typed lines are parsed (the turns files are big and kept forever)
            mine = (f'"user_id":{actor.user_id},', '"surface":"text"')
            for t in read_since(self.ctx.tracker.log, since, now, TURN_KEYS, contains=mine):
                raw = t.get("raw")
                # The asker's own typed messages only; never anyone else's
                if t.get("user_id") != actor.user_id or t.get("surface") != "text" or not isinstance(raw, str):
                    continue
                if forgotten and mentions_forgotten(raw, forgotten):
                    continue
                said = raw if len(raw) <= MAX_SAID else raw[:MAX_SAID] + "..."
                rows.append((_at(t), f"you said: {json.dumps(said, ensure_ascii=False)}"))
        if words:                                           # whole words: "on" does not match "buttons"
            rows = [r for r in rows if words <= set(re.findall(r"[a-z0-9]+", r[1].lower()))]
        rows.sort(key=lambda r: r[0], reverse=True)
        out = [f"{at.astimezone(self.tz):%a %d %b %H:%M} {line}" for at, line in rows[:MAX_LINES]]
        result: dict[str, Any] = {
            "order": "newest first", "lines": out or "nothing found", "days": days,
            "note": f"'you said' lines are {actor.name}'s own messages. Other people's messages are "
                    "private and never shown: you cannot tell what anyone else asked."}
        if len(rows) > MAX_LINES:
            result["more"] = f"{len(rows) - MAX_LINES} older lines not shown; narrow it down"
        return result

    def _who(self, e: dict[str, Any]) -> str:
        if e.get("surface") == "scheduler":
            return "a schedule"
        uid = e.get("user_id")
        if not isinstance(uid, int) or isinstance(uid, bool):
            return "unknown"
        return self.ctx.users.name_of(uid) if self.ctx.users.is_allowed(uid) else "a former user"

    def _device_line(self, e: dict[str, Any]) -> str:
        what = str(e.get("feature") or "?").replace("_", " ")
        st = e.get("ac_state")
        if isinstance(st, dict):
            try:
                what = f"set {AcState.from_dict(st).describe()}"
            except (AcStateError, TypeError, ValueError):
                pass
        via = {"button": "buttons", "slash": "a command", "llm": "chat", "scheduler": "schedule"}.get(
            str(e.get("surface")), str(e.get("surface")))
        result = "ok" if e.get("ok") else "FAILED" + (" (may have worked)" if e.get("maybe_delivered") else "")
        return f"{e.get('device')}: {what}, by {self._who(e)} via {via}: {result}"

    def _schedule_line(self, e: dict[str, Any]) -> str:
        s = e.get("after") or e.get("before") or {}
        label = s.get("label") if isinstance(s, dict) else None
        name = f"'{label}'" if label else str(e.get("sid"))
        who = e.get("name") or self._who(e)
        return f"{who} {e.get('event')} schedule {name} ({_dev(s) or '?'})"


def _dev(s: Any) -> str | None:
    return s.get("device") if isinstance(s, dict) else None


def _at(rec: dict[str, Any]) -> datetime:
    """read_since already parsed `ts` once; every line it returns has a valid one."""
    return datetime.fromisoformat(rec["ts"])

