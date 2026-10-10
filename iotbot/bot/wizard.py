"""/schedule: menu, the tap-through wizard, schedule cards, and the AC Timer button.

One message edited in place, like /keyboard. The draft lives in memory keyed by a
short id (callback data is only 64 bytes); after a restart the next tap says the
draft expired. "Type a time" catches the user's next plain text message.
Everything goes through ScheduleService (plan -> preview -> apply), the same API
the one-liner and later the LLM use.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup

from iotbot.ac.state import AcState
from iotbot.bot import acpicker as ap
from iotbot.bot.callbacks import encode
from iotbot.bot.text import b, h
from iotbot.result import Actor, Result
from iotbot.schedule import text as tx
from iotbot.schedule import timing as tm
from iotbot.schedule.model import DAYS, ID_RE, On, Schedule, Weekly

NS = "sw"
DRAFT_TTL_S = 15 * 60
MAX_DRAFTS = 50
QUICK_TIMES = ("21:30", "22:00", "22:30", "23:00", "23:30", "00:00", "07:00", "07:30")
TIMER_MINUTES = (30, 60, 120, 180)
ADJ_TEMPS = range(18, 31)
MENU_TEXT = "Schedules"
MAX_LIST = 30            # Telegram: ~100 buttons and 4096 characters per message
EXPIRED = "That draft expired (or the bot restarted); start again with /schedule."


@dataclass
class Draft:
    id: str
    user_id: int
    expires: float
    device: str | None = None
    action: dict[str, Any] | None = None
    kind: str | None = None                  # weekly | once
    time: str | None = None
    days: list[str] = field(default_factory=list)
    label: str = ""
    sid: str | None = None                   # editing this schedule
    rev: int | None = None
    plan_id: str | None = None
    quick_timer: bool = False                # Timer button: save without a preview when clean
    chat_id: int | None = None
    message_id: int | None = None
    step: str = "dev"
    history: list[str] = field(default_factory=list)
    date: Any = None                         # a once-timer on this date (else the next HH:MM)
    when_changed: bool = False               # edits only send a new time when the user set one


@dataclass
class Screen:
    text: str                                # HTML
    markup: InlineKeyboardMarkup | None = None


def _btn(label: str, *parts: object) -> Btn:
    return Btn(label, callback_data=encode(NS, *parts))


def _rows(buttons: list[Btn], cols: int) -> list[list[Btn]]:
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]


def parse_time(raw: str) -> str | None:
    """'23:00', '2330', '23', '11pm', '11:30 pm', '7am', '7.30' -> 'HH:MM' (None if not a time)."""
    t = raw.strip().lower().replace(" ", "").replace(".", ":")
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?(am|pm)?", t)
    if not m:
        return None
    hh, mm, ap_ = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap_:
        if not 1 <= hh <= 12:
            return None
        hh = hh % 12 + (12 if ap_ == "pm" else 0)
    if hh > 23 or mm > 59:
        return None
    return f"{hh:02d}:{mm:02d}"


class Wizard:
    def __init__(self, ctx: Any, clock=time.time):
        self.ctx = ctx                       # AppContext
        self._clock = clock
        self.drafts: dict[str, Draft] = {}
        self.awaiting: dict[int, str] = {}   # user id -> draft id waiting for a typed time
        self._timer_locks: dict[tuple[int, str], asyncio.Lock] = {}

    @property
    def svc(self):
        return self.ctx.schedules

    def now(self) -> datetime:
        return self.svc.now()

    # ---- drafts ----------------------------------------------------------------------------

    def new_draft(self, user_id: int, **kw: Any) -> Draft:
        now = self._clock()
        for k in [k for k, d in self.drafts.items() if d.expires < now]:
            self._drop(k)
        while len(self.drafts) >= MAX_DRAFTS:
            self._drop(next(iter(self.drafts)))
        did = "d" + secrets.token_hex(3)
        while did in self.drafts:
            did = "d" + secrets.token_hex(3)
        d = Draft(did, user_id, now + DRAFT_TTL_S, **kw)
        self.drafts[did] = d
        return d

    def get_draft(self, did: str, user_id: int) -> Draft | None:
        d = self.drafts.get(did)
        if d is None or d.user_id != user_id or d.expires < self._clock():
            return None
        d.expires = self._clock() + DRAFT_TTL_S
        return d

    def _drop(self, did: str) -> None:
        d = self.drafts.pop(did, None)
        if d and self.awaiting.get(d.user_id) == did:
            del self.awaiting[d.user_id]

    # ---- capabilities ----------------------------------------------------------------------

    def managed(self, device: str) -> bool:
        ac = self.ctx.devices.ac
        return bool(ac and ac.manages(device))

    def schedulable(self) -> list[str]:
        return [d.id for d in self.ctx.registry.devices.values() if d.features]

    def can_timer(self, device: str) -> bool:
        dev = self.ctx.devices.get(device)
        return dev is not None and (self.managed(device) or "power_off" in dev.features)

    def preset(self, device: str) -> AcState:
        ac = self.ctx.devices.ac
        last = ac.last(device) if ac else None
        if last and last.state.power:
            return last.state.with_changes(powerful=False)
        return ac.presets[device]

    # ---- menu, list, cards ---------------------------------------------------------------

    def menu(self) -> Screen:
        rows = [[_btn("+ New", "-", "new"), _btn("My schedules", "-", "list"), _btn("Tonight", "-", "tonight")],
                [_btn("Close", "-", "close")]]
        n = len(self.svc.store.all())
        text = f"{b(MENU_TEXT)}: {n} saved." if n else f"{b(MENU_TEXT)}: none yet."
        if self.svc.broken():
            text += f"\n{len(self.svc.broken())} broken (see My schedules)."
        return Screen(text, InlineKeyboardMarkup(rows))

    def list_screen(self) -> Screen:
        now = self.now()
        rows, lines = [], [b("Schedules")]
        items = self.svc.list()
        for s in items[:MAX_LIST]:
            runs = tm.next_runs(s, now, self.svc.tz)
            nxt = tx.fmt_at(runs[0], now, self.svc.tz) if runs else "no upcoming run"
            state = "" if s.enabled and not (s.paused_until and s.paused_until > now) else " (paused)"
            lines.append(f"- {h(tx.describe(s, now, self.svc.tz))}{state}; next {h(nxt)}")
            rows.append([_btn(f"{tx.name(s)} ({s.device})"[:40], "-", "s", s.id)])
        if len(items) > MAX_LIST:
            lines.append(f"... and {len(items) - MAX_LIST} more (see /schedule tonight).")
        for sid, why in list(self.svc.broken().items())[:10]:
            lines.append(f"- {h(sid[:40])}: BROKEN ({h(why[:200])})")
            if ID_RE.fullmatch(sid):
                rows.append([_btn(f"Delete broken {sid}", "-", "dropb", sid)])
            else:
                lines.append("  (odd id: remove it from schedules.json by hand)")
        if len(lines) == 1:
            lines.append("None yet.")
        rows.append([_btn("+ New", "-", "new"), _btn("<- Menu", "-", "menu")])
        return Screen("\n".join(lines), InlineKeyboardMarkup(rows))

    def card(self, sid: str, note: str = "") -> Screen:
        s = self.svc.get(sid)
        if s is None:
            return Screen(f"{note}\nSchedule {h(sid)} no longer exists.".strip(),
                          InlineKeyboardMarkup([[_btn("<- List", "-", "list")]]))
        now = self.now()
        tz = self.svc.tz
        lines = [note] if note else []
        lines.append(b(tx.describe(s, now, tz)))
        runs = tm.next_runs(s, now, tz, 3)
        lines.append("Next: " + (", ".join(h(tx.fmt_at(r, now, tz)) for r in runs) or "none"))
        if not s.enabled:
            lines.append("Paused until you resume it.")
        elif s.paused_until and s.paused_until > now:
            lines.append(f"Paused until {h(tx.fmt_at(s.paused_until, now, tz))}.")
        if s.skip_dates:
            lines.append("Skipping: " + ", ".join(h(tx.night_of(d, s.when.time)) for d in s.skip_dates
                                                 if isinstance(s.when, Weekly)))
        if s.last_fired:
            lines.append(f"Last: {h(s.last_fired.result)} {h(tx.fmt_at(s.last_fired.at, now, tz))}")
        if (why := self.svc.problem(s)):
            lines.append(f"Cannot run: {h(why)}")
        lines.append(f"id {h(s.id)}, made by {h(self.ctx.users.name_of(s.created_by))}")
        r = s.rev
        rows = []
        if not s.is_timer:
            rows.append([_btn("Skip next", "-", "skip", s.id, r),
                         _btn("Resume" if (not s.enabled or s.paused_until) else "Pause", "-",
                              "resume" if (not s.enabled or s.paused_until) else "pause", s.id, r)])
        elif not s.enabled:
            rows.append([_btn("Resume", "-", "resume", s.id, r)])
        rows.append([_btn("Edit time", "-", "edit", s.id, "time")]
                    + ([_btn("Edit settings", "-", "edit", s.id, "set")] if isinstance(s.action, On) else []))
        rows.append([_btn("Delete", "-", "del", s.id, r), _btn("<- List", "-", "list")])
        return Screen("\n".join(lines), InlineKeyboardMarkup(rows))

    def tonight(self) -> Screen:
        now = self.now()
        end = (now + timedelta(days=1)).replace(hour=12, minute=0, second=0)
        lines = [b("Until tomorrow noon")]
        for at, s, why in self.svc.agenda(now, end):
            mark = f" ({why})" if why else ""
            lines.append(f"- {h(tx.fmt_at(at, now, self.svc.tz))}: {h(tx.name(s))}, {h(s.device)} "
                         f"{h(tx.describe_action(s))}{h(mark)}")
        if len(lines) == 1:
            lines.append("Nothing scheduled.")
        return Screen("\n".join(lines), InlineKeyboardMarkup([[_btn("<- Menu", "-", "menu")]]))

    # ---- wizard steps ----------------------------------------------------------------------

    def show(self, d: Draft) -> Screen:
        """The screen for the draft's current step."""
        step = d.step
        cancel = _btn("Cancel", d.id, "x")
        back = _btn("<- Back", d.id, "back")
        nav = [back, cancel] if d.history else [cancel]
        if step == "dev":
            devs = [_btn(dev, d.id, "dev", dev) for dev in self.schedulable()]
            return Screen("New schedule: which device?", InlineKeyboardMarkup(_rows(devs, 2) + [nav]))
        if step == "act":
            if self.managed(d.device):
                opts = [_btn("Turn on", d.id, "act", "on"), _btn("Turn off", d.id, "act", "off"),
                        _btn("Change temp", d.id, "act", "adj")]
                return Screen(f"{h(d.device)}: do what?", InlineKeyboardMarkup([opts, nav]))
            dev = self.ctx.devices.get(d.device)
            feats = [_btn(f.label, d.id, "cap", f.key) for f in list(dev.features.values())[:40]]
            return Screen(f"{h(d.device)}: which button?", InlineKeyboardMarkup(_rows(feats, 2) + [nav]))
        if step == "pick":
            st = AcState.from_dict(d.action["state"]) if d.action and d.action.get("kind") == "on" \
                else self.preset(d.device)
            mk = ap.picker_keyboard(d.id, st, "Next")
            return Screen(h(ap.picker_text(d.device, st)), mk)
        if step == "adj":
            temps = [_btn(f"{t}C", d.id, "adj", t) for t in ADJ_TEMPS]
            return Screen(f"{h(d.device)}: set the temperature to (only if it is on at that time)",
                          InlineKeyboardMarkup(_rows(temps, 5) + [nav]))
        if step == "kind":
            return Screen("When?", InlineKeyboardMarkup([
                [_btn("Every week", d.id, "kind", "w"), _btn("Once (timer)", d.id, "kind", "o")], nav]))
        if step == "time":
            picks = [_btn(t, d.id, "time", t.replace(":", "")) for t in QUICK_TIMES]
            return Screen("Time? Pick one, or tap Type a time and send it (23:00, 11pm, 2330).",
                          InlineKeyboardMarkup(_rows(picks, 4) + [[_btn("Type a time", d.id, "type")], nav]))
        if step == "days":
            days = [_btn(f"[{x.title()[:2]}]" if x in d.days else x.title()[:2], d.id, "day", x,
                         0 if x in d.days else 1) for x in DAYS]
            return Screen(f"{h(d.time)} on which days? Tap to toggle, then Next.", InlineKeyboardMarkup([
                days,
                [_btn("Weekdays", d.id, "days", "wd"), _btn("Weekends", d.id, "days", "we"),
                 _btn("Every day", d.id, "days", "all")],
                [_btn("Next", d.id, "dnext")], nav]))
        if step == "prev":
            return self.preview(d)
        return Screen("Unknown step.", InlineKeyboardMarkup([[cancel]]))

    def spec(self, d: Draft) -> dict[str, Any]:
        spec: dict[str, Any] = {}
        if d.sid is None or d.device:
            spec["device"] = d.device
        if d.action is not None:
            spec["action"] = d.action
        if d.sid is not None and not d.when_changed:
            pass                               # editing settings only: keep the stored time/date
        elif d.kind == "weekly" and d.time and d.days:
            spec["when"] = {"kind": "weekly", "time": d.time, "days": [x for x in DAYS if x in d.days]}
        elif d.kind == "once" and d.time:
            at = tm.local_time(d.date, d.time, self.svc.tz) if d.date else None
            if at is None or at <= self.now():
                at = self.next_at(d.time)      # a timer's own date if still ahead, else the next HH:MM
            spec["when"] = {"kind": "once", "at": at.isoformat()}
        if d.label:
            spec["label"] = d.label
        return spec

    def next_at(self, hhmm: str) -> datetime:
        """The next time it is `hhmm` (today if still ahead, else tomorrow)."""
        now = self.now()
        at = tm.local_time(now.date(), hhmm, self.svc.tz)
        return at if at > now else tm.local_time(now.date() + timedelta(days=1), hhmm, self.svc.tz)

    def preview(self, d: Draft) -> Screen:
        actor = Actor(d.user_id, self.ctx.users.name_of(d.user_id), "button")
        r = self.svc.plan(actor, self.spec(d), sid=d.sid, rev=d.rev)
        nav = [_btn("<- Back", d.id, "back"), _btn("Cancel", d.id, "x")]
        if not r.ok:
            return Screen(h(r.message), InlineKeyboardMarkup([nav]))
        d.plan_id = r.data["plan_id"]
        lines = [b("Preview"), h(r.message)]
        lines += [f"Note: {h(w)}" for w in r.warnings]
        rows = []
        if r.conflicts:
            lines.append("Clashes with:")
            lines += [f"- {h(c['message'])}" for c in r.conflicts]
            rows.append([_btn("Keep both", d.id, "save", "k"), _btn("Replace", d.id, "save", "r")])
        elif r.data.get("passed_today"):
            lines.append(f"{h(d.time)} already passed today; the next run is the one above.")
            rows.append([_btn("Save + run now", d.id, "save", "n"), _btn("Save (from next time)", d.id, "save")])
        else:
            rows.append([_btn("Save", d.id, "save")])
        rows.append(nav)
        return Screen("\n".join(lines), InlineKeyboardMarkup(rows))

    def go(self, d: Draft, step: str) -> Screen:
        if step != d.step:
            d.history.append(d.step)
            d.step = step
        return self.show(d)

    def after_action(self, d: Draft) -> str:
        if d.sid is not None:
            return "prev"                      # editing settings only
        return "kind"

    # ---- taps ------------------------------------------------------------------------------

    async def tap(self, actor: Actor, did: str, op: str, args: list[str]) -> tuple[Screen | None, str]:
        """Handle a wizard tap. Returns (screen to show or None, popup text)."""
        d = self.get_draft(did, actor.user_id)
        if d is None:
            return None, EXPIRED              # popup only: a double tap must not wipe the "Saved" message
        if op != "type" and self.awaiting.get(actor.user_id) == d.id:
            del self.awaiting[actor.user_id]  # any other tap ends "Type a time"
        if op == "x":
            self._drop(d.id)
            return Screen("Cancelled."), ""
        steps = {"dev": "dev", "act": "act", "cap": "act", "adj": "adj", "kind": "kind", "time": "time",
                 "type": "time", "day": "days", "days": "days", "dnext": "days", "save": "prev"}
        if op in steps and d.step != steps[op]:
            return None, ""                    # a double tap on a step already left
        if op == "back":
            if not d.history:
                return self.show(d), ""
            d.step = d.history.pop()
            self.awaiting.pop(actor.user_id, None)
            return self.show(d), ""
        if op == "dev" and args and args[0] in self.schedulable():
            d.device = args[0]
            return self.go(d, "act"), ""
        if op == "act" and args and d.device and self.managed(d.device):
            if args[0] == "on":
                return self.go(d, "pick"), ""
            if args[0] == "off":
                d.action = {"kind": "off"}
                return self.go(d, self.after_action(d)), ""
            if args[0] == "adj":
                return self.go(d, "adj"), ""
        if op == "cap" and args and d.device:
            d.action = {"kind": "capture", "key": args[0]}
            return self.go(d, self.after_action(d)), ""
        if op == "adj" and args and args[0].isdigit():
            d.action = {"kind": "adjust", "changes": {"temp": int(args[0])}}
            return self.go(d, self.after_action(d)), ""
        if op == "kind" and args:
            d.kind = "weekly" if args[0] == "w" else "once"
            return self.go(d, "time"), ""
        if op == "time" and args and (t := parse_time(args[0])):
            if d.quick_timer:
                screen, note = await self.quick_timer(actor, d.device, self.next_at(t))
                if screen is not None:
                    self._drop(d.id)
                return screen, note
            return self.set_time(d, t), ""
        if op == "type":
            self.awaiting[actor.user_id] = d.id
            return None, "Send the time as a message, e.g. 23:00 or 11pm."
        if op == "day" and args and args[0] in DAYS:
            # The button says what to set (not "toggle"), so a double tap lands the same
            want = args[1] == "1" if len(args) > 1 else args[0] not in d.days
            d.days = [x for x in DAYS if (x == args[0] and want) or (x != args[0] and x in d.days)]
            return self.show(d), ""
        if op == "days" and args:
            d.days = {"wd": list(DAYS[:5]), "we": ["sat", "sun"], "all": list(DAYS)}.get(args[0], d.days)
            d.when_changed = True
            return self.show(d), ""
        if op == "dnext":
            if not d.days:
                return None, "Pick at least one day."
            return self.go(d, "prev"), ""
        if op == "save":
            return await self.save(actor, d, args[0] if args else "")
        return self.show(d), "That button does not fit this step."

    async def picker_tap(self, actor: Actor, did: str, code: str, op: str) -> tuple[Screen | None, str]:
        d = self.get_draft(did, actor.user_id)
        if d is None:
            return None, EXPIRED
        if d.step != "pick":
            return None, ""                    # a stale picker message
        if op == "x":
            self._drop(d.id)
            return Screen("Cancelled."), ""
        st = ap.decode_state(code)
        if st is None or d.device is None or not self.managed(d.device) or d.step != "pick":
            return self.show(d), "That button does not fit this step."
        if op == "noop":
            return None, f"{st.temp}C"
        if op == "done":
            d.action = {"kind": "on", "state": st.to_dict()}
            return self.go(d, self.after_action(d)), ""
        st = ap.apply_op(st, op, self.ctx.devices.ac.presets[d.device])
        d.action = {"kind": "on", "state": st.to_dict()}
        return self.show(d), ""

    def set_time(self, d: Draft, t: str) -> Screen:
        d.time = t
        d.when_changed = True
        if d.kind == "weekly":
            return self.go(d, "days")          # prefilled when editing, so days can change too
        return self.go(d, "prev")

    async def typed(self, actor: Actor, text: str) -> tuple[Draft | None, Screen | None, str]:
        """A plain message while a draft waits for a time. Returns (draft, screen, reply)."""
        did = self.awaiting.get(actor.user_id)
        if did is None:
            return None, None, ""
        self.awaiting.pop(actor.user_id, None)   # one message only: never keep catching chat
        d = self.get_draft(did, actor.user_id)
        if d is None or d.step != "time":
            return None, None, ""
        t = parse_time(text)
        if t is None:
            return d, None, "That is not a time, so I stopped waiting. Tap Type a time to try again."
        if d.quick_timer:
            screen, note = await self.quick_timer(actor, d.device, self.next_at(t))
            if screen is not None:
                self._drop(d.id)
            return d, screen, note
        return d, self.set_time(d, t), ""

    async def save(self, actor: Actor, d: Draft, mode: str) -> tuple[Screen | None, str]:
        if d.plan_id is None:
            return self.preview(d), "Check the preview first."
        resolve = {"k": "keep_both", "r": "replace"}.get(mode)
        r = await self.svc.apply(actor, d.plan_id, resolve)
        if r.error == "conflict":
            return self.preview(d), "The clashes changed; check again."
        if not r.ok:
            return Screen(h(r.message), InlineKeyboardMarkup([[_btn("<- Back", d.id, "back"),
                                                               _btn("Cancel", d.id, "x")]])), ""
        self._drop(d.id)
        await self.ctx.notifier.changed(actor, r)
        s: Schedule = r.data["schedule"]
        lines = [h(r.message)] + [f"Note: {h(w)}" for w in r.warnings]
        if mode == "n":
            ran = await self.ctx.notifier.run_now(actor, s)
            lines.append(h(ran.message))
        rows = [[_btn("Undo", "-", "undo", r.data["event_id"]), _btn("Open", "-", "s", s.id),
                 _btn("<- Menu", "-", "menu")]]
        return Screen("\n".join(lines), InlineKeyboardMarkup(rows)), ""

    # ---- menu taps (no draft) --------------------------------------------------------------

    async def menu_tap(self, actor: Actor, op: str, args: list[str]) -> tuple[Screen | None, str]:
        svc = self.svc
        arg = args[0] if args else ""
        rev = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
        if op == "menu":
            return self.menu(), ""
        if op == "close":
            return Screen("Closed. /schedule to open again."), ""
        if op == "new":
            d = self.new_draft(actor.user_id)
            return self.show(d), ""
        if op == "newfor" and arg in self.schedulable():
            d = self.new_draft(actor.user_id, device=arg, step="act")
            return self.show(d), ""
        if op == "list":
            return self.list_screen(), ""
        if op == "tonight":
            return self.tonight(), ""
        if op == "s":
            return self.card(arg), ""
        if op in ("skip", "pause", "resume"):
            fn = {"skip": lambda: svc.skip(actor, arg, rev=rev), "pause": lambda: svc.pause(actor, [arg], rev=rev),
                  "resume": lambda: svc.resume(actor, [arg], rev=rev)}[op]
            r = await fn()
            await self.ctx.notifier.changed(actor, r)
            return self.card(arg, h(r.message) if r.ok else ""), "" if r.ok else _why(r)
        if op == "del":
            s = svc.get(arg)
            if s is None:
                return self.list_screen(), "Already deleted."
            return Screen(f"Delete {h(tx.describe(s, self.now(), svc.tz))}?", InlineKeyboardMarkup([[
                _btn("Yes, delete", "-", "dely", arg, rev if rev is not None else s.rev),
                _btn("No", "-", "s", arg)]])), ""
        if op == "dely":
            r = await svc.delete(actor, arg, rev=rev)
            if not r.ok:
                return self.card(arg), _why(r)
            await self.ctx.notifier.changed(actor, r)
            return Screen(h(r.message), InlineKeyboardMarkup([[
                _btn("Undo", "-", "undo", r.data["event_id"]), _btn("<- List", "-", "list")]])), ""
        if op == "undo":
            r = await svc.revert(actor, arg)
            if not r.ok:
                return None, _why(r)          # e.g. a double tap: keep the "Reverted" message
            await self.ctx.notifier.changed(actor, r)
            return Screen(h(r.message), InlineKeyboardMarkup([[_btn("<- List", "-", "list")]])), ""
        if op == "dropb":
            r = await svc.drop_broken(actor, arg)
            return self.list_screen(), r.message
        if op == "edit":
            s = svc.get(arg)
            if s is None:
                return self.list_screen(), "That schedule no longer exists."
            local = None if isinstance(s.when, Weekly) else s.when.at.astimezone(svc.tz)
            d = self.new_draft(actor.user_id, device=s.device, sid=s.id, rev=s.rev,
                               kind="once" if s.is_timer else "weekly",
                               time=s.when.time if local is None else f"{local:%H:%M}",
                               date=None if local is None else local.date(),
                               days=list(s.when.days) if isinstance(s.when, Weekly) else [])
            if len(args) > 1 and args[1] == "set" and isinstance(s.action, On):
                d.action = s.action.to_dict()
                d.step = "pick"
            else:
                d.step = "time"
            return self.show(d), ""
        # Timer fast path (from the AC keyboard)
        if op == "tm" and self.can_timer(arg):
            opts = [_btn(_mins(m), "-", "tmin", arg, m) for m in TIMER_MINUTES]
            return Screen(f"{h(arg)} off in:", InlineKeyboardMarkup([
                opts, [_btn("At a time...", "-", "tmat", arg),
                       Btn("Cancel", callback_data=encode("kb", "d", arg))]])), ""
        if op == "tmin" and self.can_timer(arg) and rev in TIMER_MINUTES:
            at = self.now() + timedelta(minutes=rev)
            return await self.quick_timer(actor, arg, at)
        if op == "tmat" and self.can_timer(arg):
            d = self.new_draft(actor.user_id, device=arg, action={"kind": "off"}, kind="once", step="time",
                               quick_timer=True)
            return self.show(d), ""
        return self.menu(), "That button is no longer active."

    async def quick_timer(self, actor: Actor, device: str, at: datetime) -> tuple[Screen | None, str]:
        """Two taps for the common case: save at once unless something needs a decision.
        Serialized per user and device, so a double tap finds the first timer as a clash."""
        lock = self._timer_locks.setdefault((actor.user_id, device), asyncio.Lock())
        async with lock:
            return await self._quick_timer(actor, device, at)

    async def _quick_timer(self, actor: Actor, device: str, at: datetime) -> tuple[Screen | None, str]:
        spec = {"device": device, "action": {"kind": "off"}, "when": {"kind": "once", "at": at.isoformat()}}
        p = self.svc.plan(actor, spec)
        if not p.ok:
            return None, p.message
        if p.conflicts:
            d = self.new_draft(actor.user_id, device=device, action={"kind": "off"}, kind="once",
                               time=f"{at:%H:%M}", step="prev", quick_timer=True)
            return self.preview(d), ""
        r = await self.svc.apply(actor, p.data["plan_id"])
        if r.error == "conflict":
            d = self.new_draft(actor.user_id, device=device, action={"kind": "off"}, kind="once",
                               time=f"{at:%H:%M}", date=at.date(), step="prev", quick_timer=True)
            return self.preview(d), ""
        if not r.ok:
            return None, r.message
        now = self.now()
        mins = round((at - now).total_seconds() / 60)
        text = f"{h(device)} off at {h(tx.fmt_at(at, now, self.svc.tz))} (in {h(_mins(mins))})"
        return Screen(text, InlineKeyboardMarkup([[_btn("Undo", "-", "undo", r.data["event_id"])]])), ""


def _mins(m: int) -> str:
    return f"{m}m" if m < 60 else (f"{m // 60}h" if m % 60 == 0 else f"{m // 60}h{m % 60:02d}")


def _why(r: Result) -> str:
    if r.error == "stale":
        return "Already changed (maybe a double tap); here is the current version."
    return r.message
