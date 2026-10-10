"""Schedule notices: what the bot tells people when schedules run, fail, or change.

Telegram-free: builds `Notice`s (HTML text + button rows) and hands them to a
`send` coroutine the bot layer provides. Rules from PLAN.md:
- a successful run is ALWAYS silent, with [Undo] [Skip next] [Pause];
- failures, missed runs, and conflicts are audible; failures get [Retry];
- skips the user asked for say nothing; a skip because the AC is off is a silent note;
- edits by someone else notify the creator; a kept clash notifies the other owner (#15).
Undo/Retry tokens live in memory: after a restart those buttons say "expired".
"""

from __future__ import annotations

import html
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from iotbot.ac.state import AcState
from iotbot.bot.callbacks import encode
from iotbot.result import Actor, Result
from iotbot.schedule import text as tx
from iotbot.schedule.model import Capture, Schedule
from iotbot.schedule.runner import FireOutcome, Scheduler
from iotbot.schedule.service import ScheduleService

logger = logging.getLogger(__name__)

NS = "sc"            # callback namespace
UNDO_S = 10 * 60
RETRY_S = 6 * 3600
MAX_TOKENS = 200


@dataclass(frozen=True, slots=True)
class Notice:
    user_id: int
    text: str                                    # HTML
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)   # rows of (label, callback data)
    silent: bool = False


@dataclass(frozen=True, slots=True)
class Token:
    kind: str                     # undo | retry
    schedule: Schedule            # snapshot (a fired timer is already deleted)
    expires: float
    seq: int | None = None        # device action seq of the fire (Undo) or the failed send (Retry)
    prev_state: AcState | None = None
    rev: int | None = None        # schedule rev when the Retry button was made


Send = Callable[[Notice], Awaitable[None]]


def h(text: object) -> str:
    return html.escape(str(text), quote=False)


class Notifier:
    def __init__(self, service: ScheduleService, scheduler: Scheduler,
                 recipients: Callable[[int], list[int]], send: Send | None = None,
                 clock: Callable[[], float] = time.time):
        self.service = service
        self.scheduler = scheduler
        self.recipients = recipients      # creator id -> who to tell (fallback: everyone)
        self.send = send
        self._clock = clock
        self.tokens: dict[str, Token] = {}

    # ---- schedule runs ---------------------------------------------------------------------

    async def on_fire(self, o: FireOutcome) -> None:
        """Scheduler callback."""
        for n in self.fire_notices(o):
            await self._send(n)

    def fire_notices(self, o: FireOutcome) -> list[Notice]:
        s = o.schedule
        now = self.service.now()
        what = f"{h(tx.name(s))}: {h(s.device)} {h(tx.describe_action(s))}"
        rows: list[list[tuple[str, str]]] = []
        if o.result == "ok":
            first = []
            seq = o.result_obj.data.get("seq") if o.result_obj and isinstance(o.result_obj.data, dict) else None
            if o.prev_state is not None and seq is not None:
                first.append(("Undo", encode(NS, "undo", self._token("undo", s, UNDO_S, seq, o.prev_state))))
            if not s.is_timer:
                rev = self._rev(s)
                first += [("Skip next", encode(NS, "skip", s.id, rev)), ("Pause", encode(NS, "pause", s.id, rev))]
            rows = [first] if first else []
            return self._to(s, what, rows, silent=True)
        if o.result == "skipped":
            if o.reason == "skipped by request":
                return []
            return self._to(s, f"{what}\nSkipped: {h(o.reason)}", [], silent=True)
        if o.result == "superseded":
            return self._to(s, f"{what}\nNot sent: {h(o.reason)}", [], silent=True)
        if o.result == "missed":
            runs = f"{o.missed_count} runs" if o.missed_count > 1 else "it"
            text = (f"Missed {what}\nThe bot was not running, so {runs} did not happen "
                    f"(last due {h(tx.fmt_at(o.at, now, self.service.tz))}).")
            return self._to(s, text, [], silent=False)
        # failed
        row = []
        text = f"FAILED {what}\n{h(o.reason)}"
        if self._safe_to_retry(o):
            seq = o.result_obj.data.get("seq") if o.result_obj and isinstance(o.result_obj.data, dict) else None
            row.append(("Retry", encode(NS, "retry", self._token("retry", s, RETRY_S, seq, rev=self._rev(s)))))
        else:
            text += "\nIt may have run anyway; check the device before trying again (a toggle sent twice undoes itself)."
        if not s.is_timer:
            row.append(("Pause", encode(NS, "pause", s.id, self._rev(s))))
        return self._to(s, text, [row] if row else [], silent=False)

    def _safe_to_retry(self, o: FireOutcome) -> bool:
        """Full-state AC frames can always be resent; a toggle only if it surely did not run."""
        s = o.schedule
        ac = self.service.devices.ac
        if not isinstance(s.action, Capture) and ac and ac.manages(s.device):
            return True
        dev = self.service.devices.get(s.device)
        key = s.action.key if isinstance(s.action, Capture) else "power_off"
        if dev is not None and key in dev.features and dev.features[key].idempotent:
            return True
        r = o.result_obj
        maybe = r is None or not isinstance(r.data, dict) or r.data.get("maybe_delivered", True)
        return not maybe

    def _rev(self, s: Schedule) -> int:
        """Current rev (the snapshot in the outcome predates the scheduler's own write,
        which does not bump rev, but a user edit during the run would)."""
        cur = self.service.get(s.id)
        return cur.rev if cur else s.rev

    def _to(self, s: Schedule, text: str, rows: list[list[tuple[str, str]]], silent: bool) -> list[Notice]:
        return [Notice(uid, text, rows, silent) for uid in self.recipients(s.created_by)]

    def _token(self, kind: str, s: Schedule, ttl: float, seq: int | None = None,
               prev: AcState | None = None, rev: int | None = None) -> str:
        now = self._clock()
        for k in [k for k, t in self.tokens.items() if t.expires < now]:
            del self.tokens[k]
        while len(self.tokens) >= MAX_TOKENS:
            del self.tokens[next(iter(self.tokens))]
        tok = secrets.token_hex(4)
        self.tokens[tok] = Token(kind, s, now + ttl, seq, prev, rev)
        return tok

    def _take(self, tok: str, kind: str) -> Token | None:
        t = self.tokens.get(tok)
        if t is None or t.kind != kind or t.expires < self._clock():
            return None
        return t

    # ---- buttons ---------------------------------------------------------------------------

    async def undo(self, actor: Actor, tok: str) -> Result:
        """Resend the state from before the run, if the run is still the latest action."""
        t = self._take(tok, "undo")
        if t is None:
            return Result.fail("expired", "Undo is only possible for 10 minutes after a run.")
        devices = self.service.devices
        latest = devices.last.get(t.schedule.device)
        if latest is None or latest.seq != t.seq:
            who = f" by {latest.actor}" if latest else ""
            return Result.fail("superseded", f"Too late to undo: {t.schedule.device} was changed since{who}.")
        self.tokens.pop(tok, None)        # taken first: a double tap cannot send twice
        r = await devices.send_ac_state(t.schedule.device, t.prev_state, actor, source=f"undo:{t.schedule.id}")
        if r.ok:
            # IR is one-way: if someone used the handheld remote meanwhile, the bot cannot know
            r.message = f"Undone: resent {t.prev_state.describe()} to {t.schedule.device}"
        else:
            self.tokens[tok] = t           # a failed Undo can be tried again
        return r

    async def retry(self, actor: Actor, tok: str) -> Result:
        t = self._take(tok, "retry")
        if t is None:
            return Result.fail("expired", "This retry button has expired or was already used.")
        s = t.schedule
        if not s.is_timer:
            cur = self.service.get(s.id)
            if cur is None:
                return Result.fail("not_found", f"{tx.name(s)} was deleted; nothing sent.")
            if cur.rev != t.rev or not cur.enabled:
                return Result.fail("stale", f"{tx.name(s)} was changed or paused since; nothing sent.")
            s = cur
        latest = self.service.devices.last.get(s.device)
        if t.seq is not None and latest is not None and latest.seq != t.seq:
            return Result.fail("superseded", f"Not retried: {latest.actor} has used {s.device} since.")
        self.tokens.pop(tok, None)        # taken first: a double tap cannot send twice
        r = await self.run_now(actor, s)
        if not r.ok and r.error == "send_failed":
            self.tokens[tok] = t
        return r

    async def run_now(self, actor: Actor, s: Schedule) -> Result:
        """Run a schedule's action once, now (Retry, and "[Run now]" for a time that already passed)."""
        now = self.service.now()
        o = await self.scheduler.execute(s, now, f"{s.id}@now:{now.isoformat()}", actor=actor, retries=False)
        if o.result == "ok":
            return Result.success(f"Done: {s.device} {tx.describe_action(s)}")
        code = "send_failed" if o.result == "failed" else o.result
        return Result.fail(code, f"{s.device} {tx.describe_action(s)}: {o.reason}")

    # ---- changes ---------------------------------------------------------------------------

    async def changed(self, actor: Actor, r: Result) -> None:
        """After a successful service write: tell creators whose schedules someone else
        changed, and the owners of schedules a kept clash overlaps (#15)."""
        if not r.ok or not isinstance(r.data, dict):
            return
        told = set()
        for uid in r.data.get("notify") or []:
            if uid != actor.user_id:
                told.add(uid)
                await self._send(Notice(uid, f"{h(actor.name)} changed your schedule:\n{h(r.message)}"))
        for c in r.data.get("kept_conflicts") or []:
            owner = c.get("owner")
            if owner and owner != actor.user_id and owner not in told:
                told.add(owner)
                await self._send(Notice(owner, f"{h(actor.name)} added a schedule that clashes with yours: "
                                               f"{h(c.get('message', ''))}"))

    async def report_problems(self) -> None:
        """Once at start-up: schedules that cannot run (#17), and unreadable entries
        (creator unknown, so everyone is told). Never raises."""
        try:
            for s in self.service.store.all().values():
                if (why := self.service.problem(s)):
                    for n in self._to(s, f"Schedule {h(tx.name(s))} cannot run: {h(why)}. "
                                         "Edit or delete it in /schedule.", [], silent=False):
                        await self._send(n)
            broken = self.service.broken()
            if broken:
                text = "Unreadable schedules (they will not run; delete them in /schedule):\n" + "\n".join(
                    f"- {h(sid)}: {h(why)}" for sid, why in broken.items())
                for uid in self.recipients(0):
                    await self._send(Notice(uid, text))
        except Exception:  # noqa: BLE001 -- start-up must go on
            logger.exception("Could not report schedule problems")

    async def _send(self, n: Notice) -> None:
        if self.send is None:
            logger.info("Notice (no sender yet) for %s: %s", n.user_id, n.text)
            return
        try:
            await self.send(n)
        except Exception:  # noqa: BLE001 -- a notice must never break a schedule
            logger.exception("Could not send a notice to %s", n.user_id)


def fmt_dt(at: datetime, now: datetime, tz) -> str:
    return tx.fmt_at(at, now, tz)
