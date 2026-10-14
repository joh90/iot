"""Home tools for the assistant: aircons and schedules (Phase 4.4).

READ tools answer with plain data plus code-made wording (the 4.0 spike showed the model
mislabels raw numbers). WRITE tools only prepare a card line; nothing runs before the
person taps Do it. AC lines carry the exact state to send, worked out here from the bot's
record ("a bit cooler" = record - 1C), so the card shows precisely what Do it will send.

Only managed aircons (full-state encoder) are offered: toggle IR stays button-only.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from iotbot.ac.state import FANS, TEMP_MAX, TEMP_MIN, VANES, AcStateError
from iotbot.llm.cards import Done, Prepared
from iotbot.llm.tools import ArgError, Tool
from iotbot.result import Actor, Result
from iotbot.schedule import text as tx
from iotbot.schedule.model import DAYS, Schedule

if TYPE_CHECKING:
    from iotbot.context import AppContext

TIME = {"type": "string", "pattern": "[0-2][0-9]:[0-5][0-9]", "description": "24h local time HH:MM"}
DATE = {"type": "string", "pattern": "20[0-9][0-9]-[01][0-9]-[0-3][0-9]", "description": "YYYY-MM-DD"}
SID = {"type": "string", "pattern": "[A-Za-z0-9_-]{1,40}", "description": "schedule id from list_schedules"}
SETTINGS = {
    "temp": {"type": "integer", "minimum": TEMP_MIN, "maximum": TEMP_MAX},
    "fan": {"type": "string", "enum": [str(f) for f in FANS]},
    "vane": {"type": "string", "enum": [str(v) for v in VANES], "description": "swing = moving flap"},
    "powerful": {"type": "boolean"},
}
STATE_NOTE = "This is what the bot last sent. Someone may have used the physical remote since."


class AlreadyError(ArgError):
    """Nothing to change: the device is already like that (a recipe skips such a step)."""


def _setting(key: str, raw: Any) -> Any:
    """Tool strings -> AcState values ("3" -> 3 for fan/vane)."""
    return int(raw) if key in ("fan", "vane") and isinstance(raw, str) and raw.isdigit() else raw


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

    def tools(self, actor: Actor) -> list[Tool]:
        acs = self.acs()
        if not acs:
            return []
        device = {"type": "string", "enum": acs}
        return [
            Tool("get_home_state", "What the bot last sent to each aircon, when, and by whom.",
                 {"type": "object", "properties": {}}, run=self._state),
            Tool("list_schedules", "All schedules and timers with their ids, what they do, when, and the next run.",
                 {"type": "object", "properties": {"device": device}}, run=self._list),
            Tool("set_ac", "Change one aircon now. Give only what the person asked for; the rest keeps "
                           "the bot's last settings. temp_change is relative (-1 = a bit cooler). Setting temp, fan or "
                           "swing on an aircon that is off turns it on; power true alone resumes its last settings.",
                 {"type": "object", "required": ["device"], "properties": {
                     "device": device, "power": {"type": "boolean"},
                     "temp_change": {"type": "integer", "minimum": -5, "maximum": 5}, **SETTINGS}},
                 write=True, prepare=lambda a: self.prepare_ac(actor, a)),
            Tool("add_schedule", "New timer or repeating schedule. Timer (the default): in_minutes, or at "
                                 "(+ date). Repeating: at + days, only when the person said a repeat (every "
                                 "day, weekdays, Mondays). action set = change settings without on/off. Give "
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
                     "schedule_id": SID, "at": TIME, "date": DATE,
                     "days": {"type": "array", "minItems": 1, "maxItems": 7,
                              "items": {"type": "string", "enum": list(DAYS)}},
                     "action": {"type": "string", "enum": ["on", "off", "set"]},
                     "label": {"type": "string", "maxLength": 40}, **SETTINGS}},
                 write=True, prepare=lambda a: self._prep_edit(actor, a)),
            Tool("delete_schedule", "Delete a schedule or timer.",
                 {"type": "object", "required": ["schedule_id"], "properties": {"schedule_id": SID}},
                 write=True, prepare=lambda a: self._prep_simple(actor, a, "delete")),
            Tool("pause_schedule", "Pause a repeating schedule (until a date, or until resumed).",
                 {"type": "object", "required": ["schedule_id"], "properties": {"schedule_id": SID, "until": DATE}},
                 write=True, prepare=lambda a: self._prep_simple(actor, a, "pause")),
            Tool("resume_schedule", "Resume a paused schedule.",
                 {"type": "object", "required": ["schedule_id"], "properties": {"schedule_id": SID}},
                 write=True, prepare=lambda a: self._prep_simple(actor, a, "resume")),
        ]

    # ---- reads -------------------------------------------------------------------------

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
            out.append(row)
        return {"aircons": out, "note": STATE_NOTE}

    async def _list(self, args: dict[str, Any]) -> dict[str, Any]:
        svc = self.ctx.schedules
        now = svc.now()
        rows = []
        acs = self.acs()
        for s in svc.list(args.get("device")):
            if s.device not in acs:
                continue                                 # chat handles managed aircons only
            paused = s.paused_until is not None and s.paused_until > now
            status = "paused" if not s.enabled else (
                f"paused until {tx.fmt_at(s.paused_until, now, self.tz)}" if paused else "on")
            rows.append({"id": s.id, "status": status, "text": tx.describe(s, now, self.tz)})
        return {"schedules": rows or "none"}

    # ---- aircon ------------------------------------------------------------------------

    def _version(self, device: str):
        return lambda: getattr(self.ctx.devices.last.get(device), "seq", None)

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
        if before == after:
            raise AlreadyError(f"{d} is already {after.describe()} (as last sent); nothing to change")
        line = f"{before.describe() if before else 'unknown'} -> {after.describe()}"

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

            return Done(True, "ok", undo)

        item = Prepared(d, line, run, self._version(d))
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

    def _action(self, device: str, args: dict[str, Any], cur: Schedule | None = None) -> dict[str, Any] | None:
        settings = {k: _setting(k, v) for k, v in args.items() if k in SETTINGS}
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
        spec = {"device": args["device"], "action": self._action(args["device"], args), "when": when}
        if "label" in args:
            spec["label"] = args["label"]
        return self._plan_item(actor, spec, None, "new")

    async def _prep_edit(self, actor: Actor, args: dict[str, Any]) -> Prepared:
        cur = self._schedule(args["schedule_id"])
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
        action = self._action(cur.device, args, cur)
        if action is not None:
            spec["action"] = action
        if "label" in args:
            spec["label"] = args["label"]
        if not spec:
            raise ArgError("say what to change")
        return self._plan_item(actor, spec, cur, "edit")

    def _schedule(self, sid: str) -> Schedule:
        s = self.ctx.schedules.get(sid)
        if s is None or s.device not in self.acs():
            raise ArgError(f"no schedule {sid}; call list_schedules for the ids")
        return s

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
        if clash:
            line += f". Clashes with {'; '.join(clash)}; both are kept"
        if r.warnings:
            line += ". Note: " + " ".join(r.warnings)
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
        return Prepared(name, line, run, self._sched_version(cur.id if cur else None))

    async def _prep_simple(self, actor: Actor, args: dict[str, Any], op: str) -> Prepared:
        svc = self.ctx.schedules
        s = self._schedule(args["schedule_id"])
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
            raise ArgError(f"{tx.name(s)} is not paused")
        line = {"delete": "delete", "pause": f"pause until {until:%a %d %b}" if until else "pause",
                "resume": "resume"}[op] + f": {tx.describe(s, now, self.tz)}"
        rev = s.rev

        async def run(who: Actor) -> Done:
            if op == "delete":
                r = await svc.delete(who, s.id, rev)
            elif op == "pause":
                r = await svc.pause(who, [s.id], until, rev)
            else:
                r = await svc.resume(who, [s.id], rev)
            return await self._schedule_done(who, r)

        return Prepared(_sched_name(s), line, run, self._sched_version(s.id))

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
        if not change_id:
            return Done(True, "ok")

        async def undo(who2: Actor) -> Done:
            r2 = await self.ctx.schedules.revert(who2, change_id)
            if r2.ok and notifier is not None:
                await notifier.changed(who2, r2)
            return Done(True, "ok") if r2.ok else Done(False, _why(r2))

        return Done(True, "ok", undo)


def _sched_name(s: Schedule) -> str:
    return f"{'timer' if s.is_timer else 'schedule'} {s.label or s.id}"


def _why(r: Result) -> str:
    text = r.message or r.error or "failed"
    if isinstance(r.data, dict) and r.data.get("maybe_delivered"):
        text += "; it may still have worked"
    return text
