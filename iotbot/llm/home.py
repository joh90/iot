"""Home tools for the assistant: aircons and schedules (Phase 4.4).

READ tools answer with plain data plus code-made wording (the 4.0 spike showed the model
mislabels raw numbers). WRITE tools only prepare a card line; nothing runs before the
person taps Do it. AC lines carry the exact state to send, worked out here from the bot's
record ("a bit cooler" = record - 1C), so the card shows precisely what Do it will send.

Offered to chat: managed aircons (full-state encoder) and generated-IR devices whose power on
and power off are both discrete codes (a TV with a codeset). A toggle stays button-only: sent
twice it undoes itself, so chat could do the opposite of what was asked.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from iotbot.ac.state import FANS, TEMP_MAX, TEMP_MIN, VANES, AcStateError
from iotbot.devices.model import chat_tv
from iotbot.llm.cards import Done, Prepared
from iotbot.llm.tools import ORDER_ALL_RE, AlreadyError, ArgError, Tool, message_text, note_defines
from iotbot.result import Actor, Result
from iotbot.schedule import text as tx
from iotbot.schedule.model import DAYS, Schedule
from iotbot.schedule.timing import next_runs

if TYPE_CHECKING:
    from iotbot.context import AppContext

TIME = {"type": "string", "pattern": "[0-2][0-9]:[0-5][0-9]", "description": "24h local time HH:MM"}
DATE = {"type": "string", "pattern": "20[0-9][0-9]-[01][0-9]-[0-3][0-9]",
        "description": "YYYY-MM-DD, only for a later day. Leave out for today or tonight: at alone means the "
                       "next time that clock time comes"}
# edit_schedule keeps the day that is not mentioned, so a move to today needs today's date
EDIT_DATE = {**DATE, "description": "YYYY-MM-DD, timers only, only when the day changes (today too); left out, "
                                    "the day stays"}
SID = {"type": "string", "pattern": "[A-Za-z0-9_-]{1,40}", "description": "schedule id from list_schedules"}
SETTINGS = {
    "temp": {"type": "integer", "minimum": TEMP_MIN, "maximum": TEMP_MAX},
    "fan": {"type": "string", "enum": [str(f) for f in FANS]},
    "vane": {"type": "string", "enum": [str(v) for v in VANES], "description": "swing = moving flap"},
    "powerful": {"type": "boolean"},
}
STATE_NOTE = "This is what the bot last sent. Someone may have used the physical remote since."
# A TV is only pressed when the message names one, everything/all, or a note maps the words to it
# (decided 2026-11-22: "off bedroom" must never switch the TV off by guess)
TV_WORD_RE = re.compile(r"\b(tvs?|television|telly)\b", re.IGNORECASE)
ALL_RE = ORDER_ALL_RE
KIND_WORDS = {"tv", "tvs", "television", "telly", "aircon", "aircons", "air", "con", "cons", "acs"}


def _setting(key: str, raw: Any) -> Any:
    """Tool strings -> AcState values ("3" -> 3 for fan/vane)."""
    return int(raw) if key in ("fan", "vane") and isinstance(raw, str) and raw.isdecimal() else raw


def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    hours = m // 60
    return f"{hours} h ago" if hours < 48 else f"{hours // 24} days ago"


class HomeTools:
    def __init__(self, ctx: AppContext, clock=time.time):
        self.ctx = ctx
        self._clock = clock

    @property
    def tz(self):
        return self.ctx.schedules.tz

    def acs(self) -> list[str]:
        ac = self.ctx.ac
        return [d for d in self.ctx.registry.devices if ac and ac.manages(d)]

    def tvs(self) -> list[str]:
        """Generated-IR devices chat may switch: power on AND off are discrete codes."""
        return [d.id for d in self.ctx.registry.devices.values() if chat_tv(d)]

    def chat_devices(self) -> list[str]:
        return self.acs() + self.tvs()

    def tools(self, actor: Actor) -> list[Tool]:
        acs = self.chat_devices()
        if not acs:
            return []
        device = {"type": "string", "enum": acs}
        # Ids list_schedules returned in THIS turn: changes take only those, so the model cannot reuse
        # an id from an earlier message or guess one (live 2026-10-15: "remove the 10:41 timer" got
        # a card deleting the daily 12:00 schedule, the only id it had seen)
        listed: set[str] = set()
        return [
            Tool("get_home_state", f"What the bot last sent to each {'device' if self.tvs() else 'aircon'}, when, "
                                   "and by whom.",
                 {"type": "object", "properties": {}}, run=self._state),
            Tool("list_schedules", "All schedules and timers with their ids, what they do, when, and the next run.",
                 {"type": "object", "properties": {"device": device}}, run=lambda a: self._list(a, listed)),
            Tool("set_device", self._set_device_text(),
                 {"type": "object", "required": ["device"], "properties": {
                     "device": device, "power": {"type": "boolean"},
                     "temp_change": {"type": "integer", "minimum": -5, "maximum": 5}, **SETTINGS}},
                 write=True, prepare=lambda a: self.prepare_device(actor, a)),
            Tool("add_schedule", "New timer or repeating schedule. Timer (the default): in_minutes, or at "
                                 "(+ date only for a later day); a day name alone ('on Sunday', 'next Monday') "
                                 "is a timer on that date. Repeating: at + days, only when the person said a "
                                 "repeat (every day, weekdays, Mondays). action set = change settings of an "
                                 "aircon that is on, "
                                 "without on/off; for a setting at a time on an aircon that is off or may be "
                                 "off, use action on with the setting. Give "
                                 "temp, fan, vane, powerful ONLY if the person said them: left out, 'on' uses "
                                 "the room's usual settings.",
                 {"type": "object", "required": ["device", "action"], "properties": {
                     "device": device, "action": {"type": "string", "enum": ["on", "off", "set"]},
                     "at": TIME, "date": DATE,
                     "days": {"type": "array", "minItems": 1, "maxItems": 7,
                              "items": {"type": "string", "enum": list(DAYS)}},
                     "in_minutes": {"type": "integer", "minimum": 1, "maximum": 1440}, **SETTINGS}},
                 write=True, prepare=lambda a: self._prep_add(actor, a)),
            Tool("edit_schedule", "Change an existing schedule or timer. Give only what changes; label only "
                                  "when the person asks to rename it.",
                 {"type": "object", "required": ["schedule_id"], "properties": {
                     "schedule_id": SID, "at": TIME, "date": EDIT_DATE,
                     "days": {"type": "array", "minItems": 1, "maxItems": 7,
                              "items": {"type": "string", "enum": list(DAYS)}},
                     "action": {"type": "string", "enum": ["on", "off", "set"]},
                     "label": {"type": "string", "maxLength": 40}, **SETTINGS}},
                 write=True, prepare=lambda a: self._prep_edit(actor, a, listed)),
            Tool("delete_schedule", "Delete a schedule or timer.",
                 {"type": "object", "required": ["schedule_id"], "properties": {"schedule_id": SID}},
                 write=True, prepare=lambda a: self._prep_simple(actor, a, "delete", listed)),
            Tool("pause_schedule", "Pause a repeating schedule (until a date, or until resumed).",
                 {"type": "object", "required": ["schedule_id"], "properties": {"schedule_id": SID, "until": {**DATE, "description": "YYYY-MM-DD, only if the person gave a date"}}},
                 write=True, prepare=lambda a: self._prep_simple(actor, a, "pause", listed)),
            Tool("resume_schedule", "Resume a paused schedule.",
                 {"type": "object", "required": ["schedule_id"], "properties": {"schedule_id": SID}},
                 write=True, prepare=lambda a: self._prep_simple(actor, a, "resume", listed)),
        ]

    # ---- reads -------------------------------------------------------------------------

    def _set_device_text(self) -> str:
        if not self.tvs():                            # an aircon-only home keeps the Phase 9 wording
            return ("Change one aircon now. Give only what the person asked for; the rest keeps "
                    "the bot's last settings. temp_change is relative (-1 = a bit cooler). Setting temp, fan or "
                    "swing on an aircon that is off turns it on; power true alone resumes its last settings.")
        return ("Change one device now. Aircon: give only what the person asked for; the rest keeps the bot's "
                "last settings. temp_change is relative (-1 = a bit cooler). Setting temp, fan or swing on an "
                "aircon that is off turns it on; power true alone resumes its last settings. TV: power only "
                "(true = on, false = off), only when the message names the TV.")

    async def _state(self, args: dict[str, Any]) -> dict[str, Any]:
        now = self._clock()
        out = []
        for d in self.acs():
            last = self.ctx.ac.last(d)
            row: dict[str, Any] = {"device": d, "room": self.ctx.registry.devices[d].room}
            if last is None:
                row["bot_last_sent"] = "nothing yet: the bot has never set this aircon, so its state is unknown"
            else:
                at = datetime.fromtimestamp(last.at, self.tz)
                row.update(bot_last_sent=last.state.describe(), power_as_last_sent="on" if last.state.power else "off",
                           sent_at=f"{at:%a %H:%M} ({_ago(now - last.at)})", sent_via_bot_by=last.actor)
                if last.on_since is not None:
                    row["on_since"] = f"{datetime.fromtimestamp(last.on_since, self.tz):%a %H:%M}"
            out.append(row)
        result: dict[str, Any] = {"aircons": out, "note": STATE_NOTE}
        tvs = []
        for d in self.tvs():
            last = self.ctx.devices.last_power.get(d)
            row = {"device": d, "room": self.ctx.registry.devices[d].room}
            if last is None:
                row["bot_last_sent"] = "nothing yet: the bot has never switched this TV, so its state is unknown"
            else:
                at = datetime.fromtimestamp(last.at, self.tz)
                row.update(bot_last_sent="on" if last.feature == "power_on" else "off",
                           sent_at=f"{at:%a %H:%M} ({_ago(now - last.at)})", sent_via_bot_by=last.actor)
                if last.ok is not True:
                    row["send_result"] = ("still sending" if last.ok is None
                                          else "failed, may have been sent" if last.maybe else "failed")
            tvs.append(row)
        if tvs:
            result["tvs"] = tvs
        presence = getattr(self.ctx, "presence", None)
        if presence is not None and (counts := presence.counts())[1]:
            # Counts only, never names (PLAN Phase 8: visibility)
            result["people_home"] = f"{counts[0]} of {counts[1]} phones set up are on the home Wi-Fi"
        return result

    async def _list(self, args: dict[str, Any], listed: set[str] | None = None) -> dict[str, Any]:
        svc = self.ctx.schedules
        now = svc.now()
        rows = []
        acs = self.chat_devices()
        for s in svc.list(args.get("device")):
            if s.device not in acs:
                continue                                 # chat handles what it can switch only
            paused = s.paused_until is not None and s.paused_until > now
            status = "paused" if not s.enabled else (
                f"paused until {tx.fmt_at(s.paused_until, now, self.tz)}" if paused else "active")
            rows.append({"id": s.id, "status": status, "text": tx.describe(s, now, self.tz)})
            if listed is not None:
                listed.add(s.id)
        return {"schedules": rows or "none"}

    # ---- aircon ------------------------------------------------------------------------

    def _version(self, device: str):
        return lambda: getattr(self.ctx.devices.last.get(device), "seq", None)

    async def prepare_device(self, actor: Actor, args: dict[str, Any]) -> Prepared:
        if args["device"] in self.acs():
            return await self.prepare_ac(actor, args)
        return await self.prepare_button(actor, args)

    def _tv_named(self, actor: Actor, text: str) -> bool:
        """The message names a TV, says everything/all in an order, says "both" in a home of just
        two chat devices, or one of the person's notes (or a house note) ties a room or device
        word of the message to a TV ("When I say bedroom I mean the TV")."""
        if TV_WORD_RE.search(text) or ALL_RE.search(text):
            return True
        if re.search(r"\bboth\b", text, re.IGNORECASE) and len(self.chat_devices()) == 2:
            return True
        memory = getattr(self.ctx, "memory", None)
        if memory is None:
            return False
        names = {w for d in self.chat_devices() for w in re.findall(r"[a-z0-9]+", (
            d + " " + self.ctx.registry.devices[d].room).lower()) if len(w) > 2} - KIND_WORDS
        words = set(re.findall(r"[a-z0-9]+", text.lower())) & names
        notes = [f["text"] for f in memory.facts(actor.user_id)] + [f["text"] for f in memory.house()]
        return any(note_defines(w, [n for n in notes if TV_WORD_RE.search(n)]) for w in words)

    def _right_tv(self, d: str, text: str) -> bool:
        """With a room named for a TV ("off bedroom and office tv"), the TV is in a named room; with
        2+ TVs, a bare "the tv" is not enough (the model must ask which)."""
        tvs = self.tvs()
        said = set(re.findall(r"[a-z0-9]+", text.lower()))

        def rooms(t: str) -> set[str]:
            return {w for w in re.findall(r"[a-z0-9]+", (t + " " + self.ctx.registry.devices[t].room).lower())
                    if len(w) > 2} - KIND_WORDS
        named = [t for t in tvs if rooms(t) & said]
        if named:
            return d in named
        return len(tvs) == 1 or bool(ALL_RE.search(text))

    async def prepare_button(self, actor: Actor, args: dict[str, Any], named: bool = False) -> Prepared:
        """A TV (generated IR, discrete power codes): power on or off, nothing else. `named`: the
        caller already knows the TV is meant (a recipe saved with it)."""
        d = args["device"]
        extra = sorted(k for k in args if k not in ("device", "power"))
        if extra:
            raise ArgError(f"{d} can only be switched on or off; it has no {', '.join(extra)}")
        if "power" not in args:
            raise ArgError("say power true (on) or false (off)")
        said = message_text.get()
        if not named and not self._tv_named(actor, said):
            raise ArgError(f"the message does not name the TV, so {d} is not changed; if it means another "
                           "device listed for that room, use that one, else ask which device they mean")
        if not named and TV_WORD_RE.search(said) and not self._right_tv(d, said):
            raise ArgError(f"the message does not say which TV; {d} is not changed: ask which TV they mean")
        power = bool(args["power"])
        key = "power_on" if power else "power_off"
        guarded = self.ctx.registry.devices[d].features[key].guarded
        if guarded:
            # A toggle: the same rule DeviceService's guard uses (sent or maybe sent, last GUARD_HOURS);
            # a "maybe" counts, so a send that may have worked is not undone by a resend
            last = self.ctx.devices.known_power(d)
            sure = last is not None and last.ok is True
        else:
            last = self.ctx.devices.last_power.get(d)
            last, sure = (last, True) if last is not None and last.ok else (None, False)
        before = None if last is None else last.feature == "power_on"
        word = "ON" if power else "OFF"
        line = f"{'unknown' if before is None else ('' if sure else 'maybe ') + ('ON' if before else 'OFF')} -> {word}"
        # Send anyway: skip the guard, but only until a send may have gone out (Retry then must
        # not toggle it back)
        st = {"force": False}

        async def run(who: Actor) -> Done:
            r = await self.ctx.devices.run(d, key, who, source="chat", expect_seq=item.seen, force=st["force"])
            if not r.ok:
                if r.data and r.data.get("maybe_delivered"):
                    st["force"] = False
                return Done(False, _why(r))
            seq = r.data.get("seq")

            async def undo(who2: Actor) -> Done:
                back = "power_on" if before else "power_off"
                r2 = await self.ctx.devices.run(d, back, who2, source="chat:undo", expect_seq=seq)
                return Done(True, "ok") if r2.ok else Done(False, _why(r2))

            return Done(True, "ok", undo if sure and before is not power else None)

        item = Prepared(d, line, run, self._version(d))
        if before is power:
            st["force"] = True
            how = "already" if sure else "may already be"
            item.line = f"{'' if sure else 'maybe '}{word} -> {word} (send anyway)"
            item.ask, item.do_label = f"{d}: {how} {word}.", "Send anyway"
            toggle = (f". Its power key is a toggle: if it really is {word}, Send anyway turns it "
                      f"{'OFF' if power else 'ON'}" if guarded else "")
            maybe = "" if sure else " (that send may or may not have gone through)"
            raise AlreadyError(f"{d} {'is already' if sure else 'may already be'} {word} (as last sent by the "
                               f"bot, {last.actor}){maybe}. Nothing was sent; if someone used the remote, "
                               f"they can tap Send anyway{toggle}",
                               short=f"{d}: {how} {word}.", force=item)
        return item

    async def prepare_ac(self, actor: Actor, args: dict[str, Any]) -> Prepared:
        d = args["device"]
        changes = {k: _setting(k, v) for k, v in args.items() if k in SETTINGS or k == "power"}
        delta = args.get("temp_change")
        if not changes and not delta:
            raise ArgError("say what to change: power, temp, temp_change, fan, vane or powerful")
        last = self.ctx.ac.last(d)
        before = last.state if last else None
        base = before or self.ctx.ac.state_for(d, "power_on")
        if delta:
            if "temp" in changes:
                raise ArgError("give temp or temp_change, not both")
            if before is None:
                raise ArgError(f"the bot does not know {d}'s temperature yet; give an exact temp")
            changes["temp"] = base.temp + delta
        if "power" not in changes:
            changes["power"] = True                  # settings on an off aircon turn it on (decided)
        if changes["power"] is False:
            changes = {"power": False, "powerful": False}
        try:
            after = base.with_changes(**changes)
        except AcStateError as e:
            raise ArgError(str(e)) from None
        line = f"{before.describe() if before else 'unknown'} -> {after.describe()}"
        same = before == after

        async def run(who: Actor) -> Done:
            # The card's own check, repeated under the AC lock (a schedule may fire meanwhile)
            r = await self.ctx.devices.send_ac_state(d, after, who, source="chat", expect_seq=item.seen)
            if not r.ok:
                return Done(False, _why(r))
            seq = r.data.get("seq")

            async def undo(who2: Actor) -> Done:
                if before is None:
                    return Done(False, "no earlier state known")
                r2 = await self.ctx.devices.send_ac_state(d, before, who2, source="chat:undo", expect_seq=seq)
                return Done(True, "ok") if r2.ok else Done(False, _why(r2))

            return Done(True, "ok", None if same else undo)        # Send anyway: Undo would re-send the same

        item = Prepared(d, line, run, self._version(d))
        if same:
            item.line = f"{after.describe()} (send anyway)"
            said = after.describe() if after.power else "OFF"
            item.ask, item.do_label = f"{d}: already {said}.", "Send anyway"
            raise AlreadyError(f"{d} is already {after.describe()} (as last sent by the bot). Nothing was sent; "
                               "if someone used the remote, they can tap Send anyway",
                               short=f"{d}: already {said}.", force=item)
        return item

    # ---- schedules ---------------------------------------------------------------------

    def _when(self, args: dict[str, Any], weekly_ok: bool = True) -> dict[str, Any] | None:
        now = self.ctx.schedules.now()
        if "in_minutes" in args:
            if {"at", "date", "days"} & set(args):
                raise ArgError("give in_minutes, or at with date or days, not both")
            at = now + timedelta(minutes=args["in_minutes"])
            if at.second or at.microsecond:                  # round up: never sooner than asked
                at = at.replace(second=0, microsecond=0) + timedelta(minutes=1)
            return {"kind": "once", "at": at.isoformat()}
        if "days" in args:
            if "at" not in args or "date" in args:
                raise ArgError("a repeating schedule needs at and days (no date)")
            days = [d for d in DAYS if d in args["days"]]
            return {"kind": "weekly", "time": args["at"], "days": days}
        if "at" in args:
            hh, mm = map(int, args["at"].split(":"))
            if hh > 23:
                raise ArgError("at must be 00:00-23:59")
            if "date" in args:
                try:
                    day = date.fromisoformat(args["date"])
                except ValueError:
                    raise ArgError("date is not a real date") from None
            else:
                day = now.date()
            at = datetime(day.year, day.month, day.day, hh, mm, tzinfo=self.tz)
            if "date" not in args and at <= now:
                at += timedelta(days=1)                  # next occurrence
            return {"kind": "once", "at": at.isoformat()}
        if "date" in args:
            raise ArgError("a date needs at")
        return None

    def _action(self, device: str, args: dict[str, Any], cur: Schedule | None = None,
                actor: Actor | None = None) -> dict[str, Any] | None:
        settings = {k: _setting(k, v) for k, v in args.items() if k in SETTINGS}
        if device in self.tvs():
            if settings:
                raise ArgError(f"{device} can only be switched on or off; it has no {', '.join(sorted(settings))}")
            kind = args.get("action")
            if kind == "set":
                raise ArgError(f"{device} has no settings: use action on or off")
            if kind is not None and actor is not None and not self._tv_named(actor, message_text.get()):
                raise ArgError(f"the message does not name the TV, so no {device} timer is made; if it means "
                               "another device listed for that room, use that one, else ask which device they mean")
            return None if kind is None else {"kind": "off"} if kind == "off" else {"kind": "capture", "key": "power_on"}
        kind = args.get("action")
        cur_kind = cur.action.kind if cur else None
        if kind is None:
            if not settings:
                return None
            if cur_kind not in ("on", "adjust"):
                # Settings on an off schedule would silently stop it turning the aircon off
                raise ArgError(f"{tx.name(cur)} does '{tx.describe_action(cur)}'; say action on or set "
                               "to change what it does")
            kind = "set" if cur_kind == "adjust" else "on"
        if kind == "off":
            if settings:
                raise ArgError("an off action takes no settings")
            return {"kind": "off"}
        if kind == "set":
            if cur_kind == "adjust":
                settings = {**dict(cur.action.changes), **settings}   # change only what was said
            if not settings:
                raise ArgError("action set needs at least one of temp, fan, vane, powerful")
            return {"kind": "adjust", "changes": settings}
        base = self.ctx.ac.state_for(device, "power_on")
        if cur is not None and cur.action.kind == "on":
            base = cur.action.state
        try:
            state = base.with_changes(power=True, **settings)
        except AcStateError as e:
            raise ArgError(str(e)) from None
        return {"kind": "on", "state": state.to_dict()}

    async def _prep_add(self, actor: Actor, args: dict[str, Any]) -> Prepared:
        when = self._when(args)
        if when is None:
            raise ArgError("say when: in_minutes, or at (+ date or days)")
        spec = {"device": args["device"], "action": self._action(args["device"], args, actor=actor), "when": when}
        if "label" in args:
            spec["label"] = args["label"]
        return self._plan_item(actor, spec, None, "new")

    async def _prep_edit(self, actor: Actor, args: dict[str, Any], listed: set[str] | None = None) -> Prepared:
        cur = self._schedule(args["schedule_id"], listed, args)
        spec: dict[str, Any] = {}
        if {"at", "date", "days"} & set(args):
            if cur.is_timer and "days" in args:
                raise ArgError("a timer cannot become repeating; delete it and add a schedule")
            if not cur.is_timer and "date" in args:
                raise ArgError("a repeating schedule has no date; give days instead")
            w = {k: v for k, v in args.items() if k in ("at", "date", "days")}
            if cur.is_timer:
                old = cur.when.at.astimezone(self.tz)     # keep the part that was not mentioned
                w.setdefault("at", f"{old:%H:%M}")
                w.setdefault("date", f"{old:%Y-%m-%d}")
            else:
                w.setdefault("days", list(cur.when.days))
                w.setdefault("at", cur.when.time)
            spec["when"] = self._when(w)
        action = self._action(cur.device, args, cur, actor)
        if action is not None:
            spec["action"] = action
        if "label" in args:
            spec["label"] = args["label"]
        if not spec:
            raise ArgError("say what to change")
        return self._plan_item(actor, spec, cur, "edit")

    def _holiday_note(self, s: Schedule, now: datetime) -> str:
        """'Runs on a public holiday: Mon 9 Nov (Deepavali (Observed))' for the next two weeks of runs.
        Information only: schedules still run on holidays (PLAN "Holidays")."""
        hol = getattr(self.ctx, "holidays", None)
        if hol is None or not s.enabled:
            return ""
        runs = [r.astimezone(self.tz) for r in next_runs(s, now, self.tz, 14) if r - now <= timedelta(days=14)]
        hits = [(r, hol.days[r.date()]) for r in runs if r.date() in hol.days]
        if not hits:
            return ""
        return "Runs on a public holiday: " + ", ".join(f"{r:%a} {r.day} {r:%b} ({n})" for r, n in hits[:2])

    def _schedule(self, sid: str, listed: set[str] | None = None, args: dict[str, Any] | None = None) -> Schedule:
        if listed is not None and sid not in listed:
            raise ArgError("call list_schedules first and take the id from that list; ids from earlier "
                           "messages or guesses are not accepted")
        s = self.ctx.schedules.get(sid)
        if s is None or s.device not in self.chat_devices():
            raise ArgError(f"no schedule {sid}; call list_schedules for the ids")
        if listed is not None and (wrong := self._other_time(s, message_text.get(), args or {})):
            raise ArgError(wrong)
        return s

    def _other_time(self, s: Schedule, text: str, args: dict[str, Any]) -> str | None:
        """The person named a time and this schedule does not run at it (live 2026-10-15: "remove
        the bedroom timer for 10:41" offered the daily 12:00). None when no time was said, when
        they named the schedule, or when a said time matches (within 2 min)."""
        said = {m for m in said_times(text) if f"{m // 60:02d}:{m % 60:02d}" != args.get("at")}
        named = s.label and re.search(rf"\b{re.escape(s.label)}\b", text, re.IGNORECASE)   # "Bed" is not "bedroom"
        # Several targets ("the bedroom timer and the 7am one"): a time may belong to another one
        if not said or named or MANY_RE.search(text):
            return None
        if s.is_timer:
            at = s.when.at.astimezone(self.tz)
            runs = at.hour * 60 + at.minute
        else:
            hh, mm = s.when.time.split(":")
            runs = int(hh) * 60 + int(mm)
        if any(min(abs(m - runs), 1440 - abs(m - runs)) <= 2 for m in said):
            return None
        times = ", ".join(f"{m // 60:02d}:{m % 60:02d}" for m in sorted(said))
        return (f"the person said {times}, but this one runs at {runs // 60:02d}:{runs % 60:02d}: use the "
                f"schedule at that time; if there is none, change nothing and tell them what is there")

    def _plan_item(self, actor: Actor, spec: dict[str, Any], cur: Schedule | None, kind: str) -> Prepared:
        svc = self.ctx.schedules
        r = svc.plan(actor, spec, cur.id if cur else None, cur.rev if cur else None)
        if not r.ok:
            raise ArgError(r.message)
        cand: Schedule = r.data["schedule"]
        now = svc.now()
        after = tx.describe(cand, now, self.tz)
        if cur and (cand.device, cand.action, cand.when, cand.label) == (cur.device, cur.action, cur.when, cur.label):
            svc.plans.pop(r.data["plan_id"], None)
            raise ArgError(f"nothing would change: {tx.name(cur)} already does that")
        line = f"{tx.describe(cur, now, self.tz)} -> {after}" if cur else after
        clash = [str(c.get("message") or c.get("with")) for c in r.conflicts]
        parts = [line]
        if clash:
            parts.append(f"Clashes with {'; '.join(clash)}; both are kept")
        if r.warnings:
            parts.append("Note: " + " ".join(w.rstrip(".") + "." for w in r.warnings).rstrip("."))
        if (ph := self._holiday_note(cand, now)):
            parts.append(ph)
        line = ". ".join(p.rstrip(".") for p in parts)                  # no ".." between parts
        plan_id = r.data["plan_id"]
        shown = {c["with"] for c in r.conflicts}

        async def run(who: Actor) -> Done:
            r2 = await svc.apply(who, plan_id)
            if r2.error == "conflict":
                # Keep both only for exactly the clashes this card showed
                if {c["with"] for c in r2.conflicts} != shown:
                    svc.plans.pop(plan_id, None)                  # no Retry into an unseen clash
                    return Done(False, "the clashes changed since this card; ask again")
                r2 = await svc.apply(who, plan_id, "keep_both")
            return await self._schedule_done(who, r2)

        name = _sched_name(cur) if cur else f"new {'timer' if cand.is_timer else 'schedule'}"
        return Prepared(name, line, run, self._sched_version(cur.id if cur else None),
                        key=f"schedule {cur.id}" if cur else None)

    async def _prep_simple(self, actor: Actor, args: dict[str, Any], op: str,
                           listed: set[str] | None = None) -> Prepared:
        svc = self.ctx.schedules
        s = self._schedule(args["schedule_id"], listed, args)
        now = svc.now()
        until = None
        if op == "pause" and "until" in args:
            if s.is_timer:
                raise ArgError("a timer cannot pause until a date; pause it (it stays off) or delete it")
            try:
                day = date.fromisoformat(args["until"])
            except ValueError:
                raise ArgError("until is not a real date") from None
            until = datetime(day.year, day.month, day.day, tzinfo=self.tz)
            if until <= now:
                raise ArgError("until must be a future date")
        if op == "resume" and s.enabled and not (s.paused_until and s.paused_until > now):
            raise AlreadyError(f"{tx.describe(s, now, self.tz)} is not paused")
        if op == "pause" and (not s.enabled if until is None else (s.enabled and s.paused_until == until)):
            raise AlreadyError(f"{tx.describe(s, now, self.tz)} is already paused")
        line = {"delete": "delete", "pause": f"pause until {until:%a %d %b}" if until else "pause",
                "resume": "resume"}[op] + f": {tx.describe(s, now, self.tz)}" + self._made_by(s, actor)
        rev = s.rev

        async def run(who: Actor) -> Done:
            if op == "delete":
                r = await svc.delete(who, s.id, rev)
            elif op == "pause":
                r = await svc.pause(who, [s.id], until, rev)
            else:
                r = await svc.resume(who, [s.id], rev)
            return await self._schedule_done(who, r)

        return Prepared(_sched_name(s), line, run, self._sched_version(s.id), key=f"schedule {s.id}")

    def _made_by(self, s: Any, actor: Actor) -> str:
        """Someone else's schedule on your card says whose it is ("remove all my timers")."""
        if s.created_by == actor.user_id:
            return ""
        name = self.ctx.users.name_of(s.created_by)
        return f" (made by {name if name != str(s.created_by) else 'someone else'})"   # a removed user: no raw id

    def _sched_version(self, sid: str | None):
        if sid is None:
            return lambda: None                          # a new schedule: apply re-checks clashes
        return lambda: getattr(self.ctx.schedules.get(sid), "rev", "gone")

    async def _schedule_done(self, who: Actor, r: Result) -> Done:
        if not r.ok:
            return Done(False, _why(r))
        notifier = self.ctx.notifier
        if notifier is not None:
            await notifier.changed(who, r)
        change_id = r.data.get("event_id") if isinstance(r.data, dict) else None
        made = r.data.get("schedule") if isinstance(r.data, dict) else None
        timer = (made.id, made.rev) if made is not None and made.is_timer and made.enabled else None
        if not change_id:
            return Done(True, "ok", timer=timer)

        async def undo(who2: Actor) -> Done:
            r2 = await self.ctx.schedules.revert(who2, change_id)
            if r2.ok and notifier is not None:
                await notifier.changed(who2, r2)
            return Done(True, "ok") if r2.ok else Done(False, _why(r2))

        return Done(True, "ok", undo, timer)


TIME_24_RE = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")
TIME_12_RE = re.compile(r"\b(1[0-2]|0?[1-9])(?:[:.]([0-5]\d))?\s*(am|pm)\b", re.IGNORECASE)
RANGE_12_RE = re.compile(r"\b(1[0-2]|0?[1-9])(?:[:.]([0-5]\d))?\s*(?:-|to)\s*(?=(?:1[0-2]|0?[1-9])(?:[:.][0-5]\d)?\s*(am|pm)\b)",
                         re.IGNORECASE)
# Not clock times: durations ("in 2.30 hours") and dates ("until 20.10", "on 15.10")
NOT_A_TIME_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\s*(h|hr|hrs|hours?|mins?|minutes?)\b|"
                           r"\b(until|till|on|from)\s+\d{1,2}[./]\d{1,2}\b", re.IGNORECASE)
# More than one thing named: "... and ...", "both", "all"
MANY_RE = re.compile(r"\b(and|both|all|also)\b|,", re.IGNORECASE)


def said_times(text: str) -> set[int]:
    """Clock times in a message, as minutes after midnight: 10:41, 7am, 7.30pm, 7-9am (both),
    noon, midnight. Not: 2330, "in 35 mins", "in 2.30 hours", "until 20.10" (a date)."""
    text = NOT_A_TIME_RE.sub(" ", text)
    out = set()
    for m in RANGE_12_RE.finditer(text):                # "7-9am": the 7 takes the am of the 9
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        out.add(h * 60 + int(m.group(2) or 0))
    for m in TIME_12_RE.finditer(text):
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        out.add(h * 60 + int(m.group(2) or 0))
    rest = TIME_12_RE.sub(" ", RANGE_12_RE.sub(" ", text))
    out |= {int(h) * 60 + int(mm) for h, mm in TIME_24_RE.findall(rest)}
    if re.search(r"\bnoon\b", text, re.IGNORECASE):
        out.add(12 * 60)
    if re.search(r"\bmidnight\b", text, re.IGNORECASE):
        out.add(0)
    return out


def _sched_name(s: Schedule) -> str:
    """The card line's name: never the id (people read it); the line itself says what it does."""
    kind = "timer" if s.is_timer else "schedule"
    return f"{kind} {s.label}" if s.label else kind


def _why(r: Result) -> str:
    text = r.message or r.error or "failed"
    if isinstance(r.data, dict) and r.data.get("maybe_delivered"):
        text += "; it may still have worked"
    return text
