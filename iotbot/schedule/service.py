"""Schedule service: the one API for slash commands, buttons, and later the LLM.

Writes are two-phase (adversarial review #19): `plan(spec)` validates, previews
the next runs, and returns conflicts and warnings with a short-lived plan id;
`apply(plan_id)` saves it. Edits carry the schedule's `rev` (#11), so a stale
card or an LLM read-then-write race gets "changed since" plus the current
version, never a silent overwrite. Every change is logged to `schedule_events`
with before/after, and `revert(event_id)` undoes it (#20).
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from iotbot.result import Actor, Result
from iotbot.schedule import text as tx
from iotbot.schedule import timing as tm
from iotbot.schedule.model import (DAYS, Adjust, Capture, Off, On, Once, Schedule, ScheduleError,
                                   clean_label, new_id, parse_action, parse_schedule, parse_when)
from iotbot.schedule.store import ScheduleStore
from iotbot.services.devices import DeviceService
from iotbot.store import JsonlLog, StoreError

logger = logging.getLogger(__name__)

MAX_WEEKLY = 50
MAX_PLANS_PER_USER = 20
MAX_TIMERS_PER_DEVICE = 10
PLAN_TTL_S = 600
CLASH_S = 5 * 60
SIM_DAYS = 7
NEVER_OFF_S = 24 * 3600
PREVIEW_RUNS = 3


class _Abort(Exception):
    """Raised inside a store mutation to cancel it with a Result."""

    def __init__(self, result: Result):
        super().__init__(result.message)
        self.result = result


@dataclass(frozen=True, slots=True)
class Plan:
    id: str
    schedule: Schedule
    base_rev: int | None          # rev of the schedule being edited; None for a new one
    user_id: int
    expires: float
    fields: dict[str, Any]        # what the edit changes (device/action/when/label)
    seen: dict[str, int]          # conflicts the user was shown: {schedule id: rev}


class ScheduleService:
    def __init__(self, store: ScheduleStore, devices: DeviceService, events: JsonlLog,
                 tz: str | ZoneInfo, clock: Callable[[], float] = time.time,
                 on_change: Callable[[], None] | None = None):
        self.store = store
        self.devices = devices
        self.events = events
        self.tz = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
        self._clock = clock
        self.on_change = on_change          # Scheduler.wake
        self.plans: dict[str, Plan] = {}

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), self.tz).replace(microsecond=0)

    # ---- reading ---------------------------------------------------------------------------

    def get(self, sid: str) -> Schedule | None:
        return self.store.get(sid)

    def broken(self) -> dict[str, str]:
        """Entries in schedules.json that could not be read: {id: reason} (never run)."""
        return dict(self.store.broken)

    async def drop_broken(self, actor: Actor, sid: str) -> Result:
        try:
            ok = await self.store.drop_broken(sid)
        except (OSError, StoreError) as e:
            logger.error("Could not delete broken schedule %s: %s", sid, e)
            return Result.fail("store_error", "Could not save schedules.json; nothing changed.")
        if not ok:
            return Result.fail("not_found", f"'{sid}' is not a broken schedule.")
        await self._log(actor, "deleted", sid, None, None, note="broken entry removed")
        return Result.success(f"Deleted broken schedule {sid}.")

    def list(self, device: str | None = None) -> list[Schedule]:
        """Working schedules, soonest next run first (none-ahead last)."""
        now = self.now()
        far = now + timedelta(days=3650)

        def key(s: Schedule) -> tuple[datetime, str]:
            runs = tm.next_runs(s, now, self.tz)
            return (runs[0] if runs else far, s.id)

        return sorted((s for s in self.store.all().values() if device in (None, s.device)), key=key)

    def problem(self, s: Schedule) -> str | None:
        """Why this schedule cannot run as configured (device removed, encoder off, ...)."""
        dev = self.devices.get(s.device)
        if dev is None:
            return f"device '{s.device}' no longer exists"
        ac = self.devices.ac
        managed = bool(ac and ac.manages(s.device))
        a = s.action
        if isinstance(a, (On, Adjust)) and not managed:
            return f"{s.device} is not controlled by the AC encoder"
        if isinstance(a, Off) and not managed and "power_off" not in dev.features:
            return f"{s.device} has no power off"
        if isinstance(a, Capture) and a.key not in dev.features:
            return f"{s.device} has no '{a.key}'"
        return None

    def agenda(self, start: datetime, end: datetime) -> list[tuple[datetime, Schedule, str | None]]:
        """Every occurrence in (start, end], with why it will not fire (None = fires)."""
        out = [(at, s, tm.blocked(s, at, self.tz) or (self.problem(s) and "broken"))
               for s in self.store.all().values() for at in tm.due(s, start, end, self.tz)]
        return sorted(out, key=lambda x: (x[0], x[1].id))

    def _runs(self, s: Schedule, start: datetime, end: datetime) -> list[datetime]:
        return [at for at in tm.due(s, start, end, self.tz) if tm.blocked(s, at, self.tz) is None]

    def conflicts(self, cand: Schedule, exclude: Iterable[str] = (),
                  pool: dict[str, Schedule] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        """Simulate the device's next 7 days (#16). Returns (conflicts, warnings):
        conflicts are other schedules acting on the device within 5 minutes; the
        warning is an 'on' with nothing turning the device off within 24 hours."""
        now = self.now()
        first = tm.next_runs(cand, now, self.tz)
        # From the candidate's first run, so a timer 10 days out is checked too
        start = first[0] - timedelta(microseconds=1) if first else now
        end = start + timedelta(days=SIM_DAYS)
        skip = set(exclude) | {cand.id}
        others = [s for s in (pool if pool is not None else self.store.all()).values()
                  if s.device == cand.device and s.id not in skip]
        mine = self._runs(cand, start, end)
        conflicts = []
        for o in others:
            theirs = self._runs(o, now, end)
            hit = next(((a, b) for a in mine for b in theirs if abs((a - b).total_seconds()) <= CLASH_S), None)
            if hit:
                conflicts.append({
                    "kind": "clash", "with": o.id, "rev": o.rev, "owner": o.created_by,
                    "at": hit[0].isoformat(), "other_at": hit[1].isoformat(),
                    "message": f"{tx.name(o)} ({tx.describe_action(o)}) runs {tx.fmt_at(hit[1], now, self.tz)}",
                })
        warnings = []
        if mine and _turns_on(cand):
            offs = [o for o in others if _turns_off(o)]
            bare = [at for at in mine
                    if not any(self._runs(o, at, at + timedelta(seconds=NEVER_OFF_S)) for o in offs)]
            if bare:
                n = f"{len(bare)} of its runs" if len(mine) > 1 else "its run"
                warnings.append(f"Nothing turns {cand.device} off within 24 hours after {n} "
                                f"(first: {tx.fmt_at(bare[0], now, self.tz)}).")
        return conflicts, warnings

    # ---- plan / apply ----------------------------------------------------------------------

    def plan(self, actor: Actor, spec: dict[str, Any], sid: str | None = None, rev: int | None = None) -> Result:
        """Validate a new schedule (`sid` None) or an edit, and preview it.

        `spec` keys: device, action, when, label (an edit may give only some).
        Data: plan_id, schedule, next_runs (ISO), passed_today (ISO or None).
        """
        self._prune_plans()
        now = self.now()
        cur = None
        if sid is not None:
            cur = self.get(sid)
            if cur is None:
                return _not_found(sid)
            if rev is not None and cur.rev != rev:
                return _stale(cur)
        unknown = set(spec) - {"device", "action", "when", "label"}
        if unknown:
            return Result.fail("invalid", f"Unknown field(s): {', '.join(sorted(unknown))}.")
        try:
            device = spec.get("device", cur.device if cur else None)
            if not isinstance(device, str) or self.devices.get(device) is None:
                return Result.fail("device_not_found", f"Device '{device}' not found. /list shows device ids.")
            action = parse_action(spec["action"]) if "action" in spec else (cur.action if cur else None)
            when = parse_when(spec["when"]) if "when" in spec else (cur.when if cur else None)
        except ScheduleError as e:
            return Result.fail("invalid", str(e))
        if action is None or when is None:
            return Result.fail("invalid", "A schedule needs an action and a time.")
        raw_label = spec.get("label", cur.label if cur else "")
        if not isinstance(raw_label, str):
            return Result.fail("invalid", "The label must be text.")
        label = clean_label(raw_label)
        if cur and isinstance(cur.when, Once) != isinstance(when, Once):
            return Result.fail("invalid", "A timer cannot become a weekly schedule (or back); make a new one.")
        if isinstance(when, Once) and when.at <= now:
            return Result.fail("in_the_past", f"{tx.fmt_at(when.at, now, self.tz)} has already passed.")

        if cur:
            cand = replace(cur, device=device, action=action, when=when, label=label, rev=cur.rev + 1,
                           updated_by=actor.user_id, updated_at=now)
        else:
            taken = self.store.ids() | {p.schedule.id for p in self.plans.values()}
            cand = Schedule(id=new_id(isinstance(when, Once), taken), device=device, action=action, when=when,
                            label=label, created_by=actor.user_id, created_at=now,
                            updated_by=actor.user_id, updated_at=now)
        conflicts, warnings = self.conflicts(cand)
        # Clashing schedules may be replaced at apply time, so they do not count here
        # (apply checks again once it knows what stays)
        pool = {k: v for k, v in self.store.all().items() if k not in {c["with"] for c in conflicts}}
        if (err := self._check(cand, cur, pool=pool)):
            return err
        runs = tm.next_runs(cand, now, self.tz, PREVIEW_RUNS)
        # "Already passed tonight" (#13) only when the time is being set, and not if today's run happened
        passed = tm.passed_today(cand, now, self.tz) if (cur is None or cur.when != when) else None
        if passed and cur and cur.last_fired and tm.already_fired(cur, passed):
            passed = None
        mine = [p for p in self.plans.values() if p.user_id == actor.user_id]
        for old in sorted(mine, key=lambda p: p.expires)[:max(0, len(mine) - MAX_PLANS_PER_USER + 1)]:
            del self.plans[old.id]
        pid = "p" + secrets.token_hex(3)
        while pid in self.plans:
            pid = "p" + secrets.token_hex(3)
        fields = {"device": device, "action": action, "when": when, "label": label}
        plan = Plan(pid, cand, cur.rev if cur else None, actor.user_id, self._clock() + PLAN_TTL_S,
                    fields, {c["with"]: c["rev"] for c in conflicts})
        self.plans[plan.id] = plan
        nxt = ", ".join(tx.fmt_at(r, now, self.tz) for r in runs) or "none"
        return Result.success(
            f"{tx.describe(cand, now, self.tz)}\nNext: {nxt}",
            data={"plan_id": plan.id, "schedule": cand, "next_runs": [r.isoformat() for r in runs],
                  "passed_today": passed.isoformat() if passed else None},
            warnings=warnings, conflicts=conflicts)

    def _check(self, cand: Schedule, cur: Schedule | None,
               pool: dict[str, Schedule] | None = None) -> Result | None:
        """Device/action fit, label unique per device, limits. `pool` is the working set
        (inside a store mutation); `cur` is the version being replaced, if any."""
        pool = pool if pool is not None else self.store.all()
        if (why := self.problem(cand)):
            return Result.fail("not_supported", f"Cannot schedule that: {why}.")
        if cand.label and any(s.id != cand.id and s.device == cand.device
                              and s.label.casefold() == cand.label.casefold() for s in pool.values()):
            return Result.fail("label_taken", f"{cand.device} already has a schedule called '{cand.label}'.")
        others = [s for s in pool.values() if s.id != cand.id]
        broken = [sid for sid in self.store.broken if sid != cand.id]
        if cand.is_timer:
            if cur is None or cur.device != cand.device:
                n = sum(1 for s in others if s.is_timer and s.device == cand.device)
                if n >= MAX_TIMERS_PER_DEVICE:
                    return Result.fail("limit", f"{cand.device} already has {n} timers "
                                                f"(max {MAX_TIMERS_PER_DEVICE}).")
        elif cur is None:
            n = sum(1 for s in others if not s.is_timer) + sum(1 for sid in broken if sid.startswith("s"))
            if n >= MAX_WEEKLY:
                return Result.fail("limit", f"There are already {MAX_WEEKLY} schedules; delete one first.")
        return None

    async def apply(self, actor: Actor, plan_id: str, resolve: str | None = None) -> Result:
        """Save a plan. With conflicts, `resolve` must be 'keep_both' or 'replace'.
        Replace deletes only the clashing schedules the user was shown, and only if
        they have not changed since; any new or changed clash asks again."""
        self._prune_plans()
        plan = self.plans.get(plan_id)
        if plan is None:
            return Result.fail("plan_expired", "That preview expired; start again.")
        if plan.user_id != actor.user_id:
            return Result.fail("forbidden", "That preview belongs to someone else.")
        if resolve not in (None, "keep_both", "replace"):
            return Result.fail("invalid", f"Unknown conflict choice '{resolve}'.")
        now = self.now()
        conflicts, warnings = self.conflicts(plan.schedule)
        current = {c["with"]: c["rev"] for c in conflicts}
        if conflicts and (resolve is None or current != plan.seen):
            # Ask (again): either nothing was chosen, or the clashes changed since the preview
            self.plans[plan_id] = replace(plan, seen=current)
            return Result.fail("conflict", "This clashes with another schedule.", conflicts=conflicts,
                               data={"plan_id": plan_id})
        replaced = dict(current) if resolve == "replace" else {}

        def apply(d: dict[str, Schedule]) -> tuple[Schedule, dict | None, list[dict[str, Any]]]:
            cur = d.get(plan.schedule.id)
            if plan.base_rev is None:
                if cur is not None or plan.schedule.id in self.store.broken:
                    raise _Abort(Result.fail("id_taken", "That id was taken meanwhile; start again."))
                cand = replace(plan.schedule, created_at=now, updated_at=now)
            elif cur is None:
                raise _Abort(_not_found(plan.schedule.id))
            elif cur.rev != plan.base_rev:
                raise _Abort(_stale(cur))
            else:
                # Built from the stored version: keeps what the scheduler wrote since the
                # preview (last_fired, consumed skip dates)
                cand = replace(cur, **plan.fields, rev=cur.rev + 1, updated_by=actor.user_id, updated_at=now)
            # Again on the copy being saved: two saves at the same moment must not both
            # slip past the clash check (double-tapped timer buttons)
            again, _ = self.conflicts(cand, pool=d)
            if again and (resolve is None or {c["with"]: c["rev"] for c in again} != current):
                raise _Abort(Result.fail("conflict", "This clashes with another schedule.", conflicts=again,
                                         data={"plan_id": plan_id}))
            gone = []
            for sid, rev in replaced.items():
                other = d.get(sid)
                if other is None or other.rev != rev:
                    raise _Abort(Result.fail("conflict", "A clashing schedule changed meanwhile; check again.",
                                             data={"plan_id": plan_id}))
                gone.append(d.pop(sid).to_dict())
            if (err := self._check(cand, cur, pool=d)):
                raise _Abort(err)
            d[cand.id] = cand
            return cand, (cur.to_dict() if cur else None), gone

        try:
            cand, before, gone = await self.store.mutate(apply)
        except _Abort as e:
            return e.result
        except (OSError, StoreError, ScheduleError) as e:
            logger.error("Could not save schedule %s: %s", plan.schedule.id, e)
            return Result.fail("store_error", "Could not save schedules.json; nothing changed.")
        self.plans.pop(plan_id, None)
        batch = _batch_id()
        for g in gone:
            await self._log(actor, "deleted", g["id"], g, None, note=f"replaced by {cand.id}", batch=batch)
        await self._log(actor, "updated" if before else "created", cand.id, before, cand.to_dict(), batch=batch)
        self._changed()
        verb = "Updated" if before else "Saved"
        msg = f"{verb} {cand.id}: {tx.describe(cand, now, self.tz)}"
        if gone:
            msg += f"\nReplaced {', '.join(g['id'] for g in gone)}."
        return Result.success(msg, data={
            "schedule": cand, "event_id": batch, "notify": self._notify_list(actor, before, *gone),
            "kept_conflicts": conflicts if resolve == "keep_both" else []}, warnings=warnings)

    # ---- small edits -------------------------------------------------------------------------

    async def delete(self, actor: Actor, sid: str, rev: int | None = None) -> Result:
        return await self._edit(actor, [sid], rev, lambda s: None, "deleted", "Deleted")

    async def pause(self, actor: Actor, sids: list[str], until: datetime | None = None,
                    rev: int | None = None) -> Result:
        """Pause indefinitely, or until a resume time (#12, always echoed back).
        Timers have no pause: they are disabled."""
        now = self.now()
        if until is not None and until <= now:
            return Result.fail("in_the_past", "The resume time has already passed.")

        def change(s: Schedule) -> Schedule:
            if until is None or s.is_timer:
                return replace(s, enabled=False)
            return replace(s, enabled=True, paused_until=until)

        how = f"until {tx.fmt_at(until, now, self.tz)}" if until else "until you resume"
        return await self._edit(actor, sids, rev, change, "paused", f"Paused {how}:")

    async def resume(self, actor: Actor, sids: list[str], rev: int | None = None) -> Result:
        return await self._edit(actor, sids, rev, lambda s: replace(s, enabled=True, paused_until=None),
                                "resumed", "Resumed")

    async def skip(self, actor: Actor, sid: str, day: date | None = None, rev: int | None = None) -> Result:
        """Skip the next run, or the run on `day` (its local fire date). Pass the `rev` the
        button was made for, so a double tap cannot skip two nights."""
        s = self.get(sid)
        if s is None:
            return _not_found(sid)
        if s.is_timer:
            return Result.fail("invalid", "Timers cannot skip; delete the timer instead.")
        now = self.now()
        if day is None:
            day = tm.fire_date_for_skip(s, now, self.tz)
            if day is None:
                return Result.fail("nothing_to_skip", f"{tx.name(s)} has no upcoming run.")
        elif day < now.date() or DAYS[day.weekday()] not in s.when.days:
            return Result.fail("invalid", f"{tx.name(s)} does not run on {day:%a %d %b}.")
        shown = tx.night_of(day, s.when.time)

        def change(cur: Schedule) -> Schedule:
            return replace(cur, skip_dates=tuple(sorted(set(tm.live_skip_dates(cur, now.date())) | {day})))

        return await self._edit(actor, [sid], rev, change, "skipped", f"Skipping {shown}:")

    async def unskip(self, actor: Actor, sid: str, day: date, rev: int | None = None) -> Result:
        def change(cur: Schedule) -> Schedule:
            return replace(cur, skip_dates=tuple(d for d in cur.skip_dates if d != day))

        return await self._edit(actor, [sid], rev, change, "unskipped", "No longer skipping:")

    def owned_by(self, user_id: int) -> list[Schedule]:
        return [s for s in self.list() if s.created_by == user_id]

    async def reassign(self, actor: Actor, keep: Callable[[int], bool], to_user: int) -> Result:
        """Hand every schedule whose creator is no longer approved (`keep` False) to
        `to_user` (#14). Run on each user removal, so a failed earlier hand-over and
        older orphans are picked up too. Ownership is bookkeeping: rev is not bumped,
        so open buttons on those schedules keep working."""
        def apply(d: dict[str, Schedule]) -> list[tuple[str, dict, Schedule]]:
            out = []
            for sid, s in list(d.items()):     # chosen here: a timer that just fired is simply gone
                if s.created_by != 0 and not keep(s.created_by):
                    new = replace(s, created_by=to_user)
                    d[sid] = new
                    out.append((sid, s.to_dict(), new))
            return out

        try:
            changed = await self.store.mutate(apply)
        except (OSError, StoreError, ScheduleError) as e:
            logger.error("Could not hand over schedules to %s: %s", to_user, e)
            return Result.fail("store_error", "Could not save schedules.json; they will be handed over at "
                                              "the next user removal.")
        batch = _batch_id()
        for sid, before, new in changed:
            await self._log(actor, "reassigned", sid, before, new.to_dict(), batch=batch)
        return Result.success(f"Handed over {len(changed)} schedule(s).",
                              data={"ids": [sid for sid, _, _ in changed], "event_id": batch})

    def select(self, target: str) -> list[str]:
        """Ids for bulk actions: 'all', a device id, or one schedule id (#22)."""
        if target == "all":
            return sorted(self.store.all())
        if self.devices.get(target) is not None:
            return [s.id for s in self.list(target)]
        return [target] if self.get(target) else []

    async def _edit(self, actor: Actor, sids: list[str], rev: int | None,
                    change: Callable[[Schedule], Schedule | None], event: str, verb: str) -> Result:
        if not sids:
            return Result.fail("nothing_selected", "No matching schedules.")
        now = self.now()

        def apply(d: dict[str, Schedule]) -> list[tuple[str, dict | None, Schedule | None]]:
            out = []
            for sid in sids:
                cur = d.get(sid)
                if cur is None:
                    raise _Abort(_not_found(sid))
                if rev is not None and cur.rev != rev:
                    raise _Abort(_stale(cur))
                new = change(cur)
                if new == cur:
                    continue
                if new is None:
                    del d[sid]
                else:
                    new = replace(new, rev=cur.rev + 1, updated_by=actor.user_id, updated_at=now)
                    d[sid] = new
                out.append((sid, cur.to_dict(), new))
            return out

        try:
            changed = await self.store.mutate(apply)
        except _Abort as e:
            return e.result
        except (OSError, StoreError, ScheduleError) as e:
            logger.error("Could not save schedules: %s", e)
            return Result.fail("store_error", "Could not save schedules.json; nothing changed.")
        batch = _batch_id()
        for sid, before, new in changed:
            await self._log(actor, event, sid, before, new.to_dict() if new else None, batch=batch)
        if changed:
            self._changed()
        names = ", ".join(tx.name(new) if new else sid for sid, _, new in changed) or "nothing to change"
        return Result.success(f"{verb} {names}", data={
            "ids": [sid for sid, _, _ in changed], "event_id": batch if changed else None,
            "notify": self._notify_list(actor, *[b for _, b, _ in changed])})

    # ---- history ---------------------------------------------------------------------------

    async def _log(self, actor: Actor, event: str, sid: str, before: dict | None, after: dict | None,
                   note: str | None = None, batch: str | None = None) -> str:
        """One line per schedule change. Changes made together share a `batch` id,
        which is what Undo/revert takes."""
        event_id = "e" + secrets.token_hex(4)
        await self.events.append_async({
            "event_id": event_id, "batch": batch or event_id, "event": event, "sid": sid,
            "user_id": actor.user_id, "name": actor.name, "surface": actor.surface,
            "before": before, "after": after, "note": note}, when=self.now())
        return event_id

    def find_events(self, change_id: str) -> list[dict[str, Any]]:
        """The events of one change (a batch id, or a single event id), oldest first."""
        now = self.now()
        prev = (now.replace(day=1) - timedelta(days=1))
        out = []
        for y, m in ((prev.year, prev.month), (now.year, now.month)):
            out += [rec for rec in self.events.read_month(y, m)
                    if change_id in (rec.get("batch"), rec.get("event_id"))]
        return out

    async def revert(self, actor: Actor, change_id: str) -> Result:
        """Undo one change (all schedules it touched), if none of them changed since (#20).
        All or nothing; the restored schedules pass the same checks as a new save."""
        evs = self.find_events(change_id)
        if not evs:
            return Result.fail("change_not_found", "That change is too old or unknown.")
        now = self.now()
        try:
            restored = {ev["sid"]: (parse_schedule(ev["before"]) if ev.get("before") else None) for ev in evs}
        except ScheduleError as e:
            return Result.fail("invalid", f"Cannot restore: {e}")
        afters = {ev["sid"]: ev.get("after") for ev in evs}

        def apply(d: dict[str, Schedule]) -> list[tuple[str, dict | None, Schedule | None]]:
            out = []
            for sid, after in afters.items():
                cur = d.get(sid)
                if after is None:
                    if cur is not None or sid in self.store.broken:
                        raise _Abort(Result.fail("changed_since", f"{sid} exists again; nothing reverted."))
                elif cur is None or cur.rev != after.get("rev"):
                    raise _Abort(Result.fail("changed_since", f"{sid} changed after that; nothing reverted.",
                                             data={"schedule": cur}))
            for sid in afters:
                cur, old = d.get(sid), restored[sid]
                if old is None:
                    d.pop(sid, None)
                    out.append((sid, cur.to_dict() if cur else None, None))
                    continue
                new = replace(old, rev=max(old.rev, cur.rev if cur else 0) + 1, updated_by=actor.user_id,
                              updated_at=now, last_fired=cur.last_fired if cur else old.last_fired)
                if new.is_timer and new.when.at <= now:
                    raise _Abort(Result.fail("in_the_past", f"Timer {sid} would already have fired."))
                d[sid] = new
                out.append((sid, cur.to_dict() if cur else None, new))
            for _, _, new in out:
                if new is not None and (err := self._check(new, None if new.id not in self.store.all() else
                                                           self.store.get(new.id), pool=d)):
                    raise _Abort(err)
            return out

        try:
            changed = await self.store.mutate(apply)
        except _Abort as e:
            return e.result
        except (OSError, StoreError, ScheduleError) as e:
            logger.error("Could not revert %s: %s", change_id, e)
            return Result.fail("store_error", "Could not save schedules.json; nothing changed.")
        batch = _batch_id()
        for sid, before, new in changed:
            await self._log(actor, "reverted", sid, before, new.to_dict() if new else None,
                            note=f"reverts {change_id}", batch=batch)
        self._changed()
        parts = [f"restored {tx.describe(new, now, self.tz)}" if new else f"removed {sid}"
                 for sid, _, new in changed]
        news = [new for _, _, new in changed if new]
        return Result.success("Reverted: " + "; ".join(parts), data={
            "schedule": news[0] if len(news) == 1 else None, "event_id": batch,
            "notify": self._notify_list(actor, *[b for _, b, _ in changed],
                                        *[n.to_dict() for _, _, n in changed if n])})

    # ---- helpers ---------------------------------------------------------------------------

    def _notify_list(self, actor: Actor, *befores: dict | None) -> list[int]:
        """Creators of schedules someone else changed (plan: edits by others notify the creator)."""
        out = {b["created_by"] for b in befores if b and b.get("created_by") not in (None, actor.user_id, 0)}
        return sorted(out)

    def _changed(self) -> None:
        if self.on_change:
            self.on_change()

    def _prune_plans(self) -> None:
        now = self._clock()
        for pid in [p.id for p in self.plans.values() if p.expires < now]:
            del self.plans[pid]


def _turns_on(s: Schedule) -> bool:
    return isinstance(s.action, On) or (isinstance(s.action, Capture) and s.action.key.startswith("power_on"))


def _turns_off(s: Schedule) -> bool:
    return isinstance(s.action, Off) or (isinstance(s.action, Capture) and s.action.key == "power_off")


def _batch_id() -> str:
    return "c" + secrets.token_hex(4)


def _not_found(sid: str) -> Result:
    return Result.fail("not_found", f"No schedule '{sid}'.")


def _stale(cur: Schedule) -> Result:
    return Result.fail("stale", f"{tx.name(cur)} was changed meanwhile (now rev {cur.rev}); here is the current "
                                "version.", data={"schedule": cur})
