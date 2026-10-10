"""The scheduler loop: fires due schedules, reports missed ones, retries failures.

Design (PLAN.md adversarial review #1, #4, #5, #10):
- Own loop, no PTB run_daily: sleep until the next run (at most TICK_MAX), then
  handle every occurrence in (cursor, now]. Clock jumps cannot double-fire: an
  occurrence at or before a schedule's `last_fired` is never fired again.
- `last_fired` is saved as "started" BEFORE sending, so a restart mid-send never
  repeats it, then updated with the result.
- Occurrences more than GRACE late (bot down, clock jump) are not sent; each
  schedule reports them once as missed.
- Clock guard (the Pi has no RTC; at boot the clock can be years off until NTP
  syncs): nothing fires until the kernel says NTP is synced. If the kernel
  cannot tell, or NTP is not synced after CLOCK_WAIT_MAX_S, the clock must at
  least be past the newest time the bot ever saved.
- Retries: AC state frames up to 3 times over 60s; captured codes only when
  nothing was delivered. A newer action on the device cancels pending retries.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from zoneinfo import ZoneInfo

from iotbot.ac.state import AcState
from iotbot.result import SYSTEM_USER_ID, Actor, Result
from iotbot.schedule import timing as tm
from iotbot.schedule.model import Adjust, LastFired, Off, On, Once, Schedule
from iotbot.schedule.store import ScheduleStore
from iotbot.services.devices import DeviceService

logger = logging.getLogger(__name__)

GRACE_S = 120
TICK_MAX_S = 60
CLOCK_POLL_S = 30
CLOCK_WAIT_MAX_S = 30 * 60
TIME_ERROR = 5   # adjtimex() return value: clock not synchronized
RETRY_DELAYS_S = (20, 20, 20)


@dataclass(frozen=True, slots=True)
class FireOutcome:
    """What happened to one occurrence; the notifier (2.5) turns it into messages."""
    schedule: Schedule
    at: datetime                      # planned fire time
    key: str
    result: str                       # ok | failed | superseded | missed | skipped
    reason: str = ""                  # why skipped / failed / how many missed
    result_obj: Result | None = None  # last DeviceService result
    attempts: int = 0
    prev_state: AcState | None = None  # bot-known AC state before this fire (for Undo)
    missed_count: int = 0


Notify = Callable[[FireOutcome], Awaitable[None]]


def ntp_synced() -> bool | None:
    """Kernel NTP state via adjtimex(modes=0) (read only, no privileges needed):
    True synced, False not synced, None if it cannot be read."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        buf = ctypes.create_string_buffer(512)     # struct timex, zeroed: modes = 0
        state = libc.adjtimex(buf)
    except (OSError, AttributeError):
        return None
    if state < 0:
        return None
    return state != TIME_ERROR


def schedule_actor(s: Schedule) -> Actor:
    return Actor(SYSTEM_USER_ID, f"schedule {s.label or s.id}", "scheduler")


class Scheduler:
    def __init__(self, store: ScheduleStore, devices: DeviceService, tz: str | ZoneInfo,
                 notify: Notify | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 floor: Callable[[], float | None] | None = None,
                 synced: Callable[[], bool | None] = ntp_synced):
        self.store = store
        self.devices = devices
        self.tz = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
        self.notify = notify
        self._clock = clock
        self._sleep = sleep
        self._floor = floor            # extra "newest saved time" sources (e.g. AC state)
        self._synced = synced
        self.clock_state = "starting"  # shown in /status
        self._cursor: datetime | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.inflight: set[asyncio.Task] = set()
        self._unsaved: dict[str, FireOutcome] = {}   # final outcomes whose save failed, retried each tick

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), self.tz)

    # ---- lifecycle -----------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run_forever(), name="scheduler")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self.inflight) if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None

    def wake(self) -> None:
        """Call after any schedule change so the loop recomputes its next wake-up."""
        self._wake.set()

    async def run_forever(self) -> None:
        # Nothing in here may end the loop: any error is logged and retried
        while True:
            try:
                await self.wait_for_sane_clock()
                await self.recover_started()
                break
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Scheduler start-up failed; retrying")
                await self._sleep(CLOCK_POLL_S)
        if not self.clock_state.startswith("saved times"):
            self.clock_state = "ok"
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Scheduler tick failed")
            self._wake.clear()
            try:
                delay = self.seconds_to_next()
            except Exception:  # noqa: BLE001
                logger.exception("Could not work out the next run")
                delay = TICK_MAX_S
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except TimeoutError:
                pass

    def clock_floor(self) -> float | None:
        stamps = [self.store.latest_timestamp(), self._floor() if self._floor else None]
        return max((s for s in stamps if s is not None), default=None)

    async def wait_for_sane_clock(self) -> None:
        """Return once the clock can be trusted (see module docstring)."""
        waited = 0.0
        logged = None
        while True:
            synced = self._synced()
            floor = self.clock_floor()
            behind = floor is not None and self._clock() < floor
            if synced:
                if behind:
                    ahead = datetime.fromtimestamp(floor, self.tz)
                    logger.error("NTP is synced but the bot saved times later than now (%s > %s); "
                                 "trusting NTP", ahead.isoformat(), self.now().isoformat())
                    # Runs already marked fired after now stay blocked until then (that is what
                    # stops a double fire after a backwards correction), so say so in /status
                    self.clock_state = f"saved times are ahead of the clock (until {ahead:%a %d %b %H:%M})"
                    return
                break
            if not behind and (synced is None or waited >= CLOCK_WAIT_MAX_S):
                if synced is False:
                    logger.warning("NTP still not synced after %d min; running schedules on the "
                                   "current clock (%s)", waited // 60, self.now().isoformat())
                break
            state = "clock behind the last saved time" if behind else "waiting for NTP"
            if state != logged:
                logger.warning("Schedules on hold: %s (clock %s)", state, self.now().isoformat())
                logged = state
            self.clock_state = state
            await self._sleep(CLOCK_POLL_S)
            waited += CLOCK_POLL_S
        if logged:
            logger.info("Clock trusted again; starting schedules")

    async def recover_started(self) -> None:
        """A run still marked "started" means the bot stopped mid-send: report it as
        failed (it may or may not have reached the AC) and never resend it."""
        for s in self.store.all().values():
            lf = s.last_fired
            if lf and lf.result == "started":
                at = tm.key_time(lf.occurrence) or lf.at
                o = FireOutcome(s, at, lf.occurrence, "failed", "the bot restarted while sending; it may not have run")
                await self._record(o)
                await self._notify(o)

    def seconds_to_next(self) -> float:
        now = self.now()
        nxt = [r[0] for s in self.store.all().values() if (r := tm.next_runs(s, now, self.tz))]
        if not nxt:
            return TICK_MAX_S
        return max(0.05, min(TICK_MAX_S, (min(nxt) - now).total_seconds()))

    # ---- one pass ------------------------------------------------------------------------

    def window_start(self, s: Schedule) -> datetime:
        """Occurrences at or before this are done: last fired, last edited, or already handled."""
        starts = [s.updated_at]
        if s.last_fired and (t := tm.key_time(s.last_fired.occurrence, s.id)):
            starts.append(t)
        if self._cursor:
            starts.append(self._cursor)
        return max(starts)

    async def tick(self) -> list[asyncio.Task]:
        """Handle every occurrence up to now. Returns the fire tasks it started."""
        now = self.now()
        started = []
        try:
            for s in self.store.all().values():
                try:   # one bad schedule must not stop the others
                    occ = tm.due(s, self.window_start(s), now, self.tz)
                    if not occ:
                        continue
                    late = [a for a in occ
                            if (now - a).total_seconds() > GRACE_S and tm.blocked(s, a, self.tz) is None]
                    fresh = [a for a in occ if (now - a).total_seconds() <= GRACE_S]
                    at = fresh[-1] if fresh else occ[-1]
                    started.append(self._spawn(self.handle(s, at, now, late, fire=bool(fresh))))
                except Exception:  # noqa: BLE001
                    logger.exception("Could not handle schedule %s", s.id)
        finally:
            # Advanced even if the pass failed half way: in-flight runs must not respawn
            self._cursor = max(self._cursor or now, now)
        await self.retry_unsaved()
        await self.sweep_timers(now)
        return started

    async def retry_unsaved(self) -> None:
        """Save outcomes whose first save failed; otherwise a restart would report a
        run that worked as "may not have run" (and a timer would linger)."""
        for key, o in list(self._unsaved.items()):
            if await self._record(o):
                self._unsaved.pop(key, None)

    def _dead_timer(self, s: Schedule, now: datetime) -> bool:
        """A timer past its time that will never fire: blocked (disabled), or already
        has a final outcome that a failed delete left behind."""
        return (isinstance(s.when, Once) and (now - s.when.at).total_seconds() > GRACE_S
                and bool(tm.blocked(s, s.when.at, self.tz)
                         or (s.last_fired and s.last_fired.result != "started")))

    async def sweep_timers(self, now: datetime) -> None:
        if not any(self._dead_timer(s, now) for s in self.store.all().values()):
            return

        def apply(d: dict[str, Schedule]) -> list[str]:
            # Checked again on the copy being saved: an edit queued meanwhile wins
            dead = [sid for sid, s in d.items() if self._dead_timer(s, now)]
            for sid in dead:
                del d[sid]
            return dead

        try:
            dead = await self.store.mutate(apply)
            if dead:
                logger.info("Removed expired timers: %s", ", ".join(dead))
        except Exception:  # noqa: BLE001
            logger.exception("Could not remove expired timers")

    def _spawn(self, coro: Awaitable[None]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self.inflight.add(task)
        task.add_done_callback(self.inflight.discard)
        return task

    async def handle(self, s: Schedule, at: datetime, now: datetime, missed: list[datetime], fire: bool) -> None:
        """Report missed runs, then fire `at` if it is fresh and not skipped/paused."""
        try:
            if missed:
                last = missed[-1]
                outcome = FireOutcome(s, last, tm.occurrence_key(s, last, self.tz), "missed",
                                      f"bot was not running at {last:%a %H:%M}", missed_count=len(missed))
                if not fire:
                    await self._record(outcome)
                await self._notify(outcome)
            if not fire:
                return
            key = tm.occurrence_key(s, at, self.tz)
            why = tm.blocked(s, at, self.tz)
            if why == "skipped":
                outcome = FireOutcome(s, at, key, "skipped", "skipped by request")
                await self._record(outcome)
                await self._notify(outcome)
                return
            if why:   # disabled or paused: nothing to record or say
                return
            if not await self._record(FireOutcome(s, at, key, "started")):
                # Without the marker a restart could send it again: do not send at all
                outcome = FireOutcome(s, at, key, "failed", "could not save the schedule state, so it was "
                                      "not sent")
                await self._notify(outcome)
                return
            outcome = await self.execute(s, at, key)
            if not await self._record(outcome):
                self._unsaved[key] = outcome
            await self._notify(outcome)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Schedule %s at %s failed unexpectedly", s.id, at)

    # ---- sending -------------------------------------------------------------------------

    async def execute(self, s: Schedule, at: datetime, key: str, actor: Actor | None = None,
                      retries: bool = True) -> FireOutcome:
        """Send the schedule's action. `actor`/`retries` let a person re-run it (Retry, Run now)."""
        dev = self.devices.get(s.device)
        if dev is None:
            return FireOutcome(s, at, key, "failed", f"device '{s.device}' no longer exists")
        ac = self.devices.ac
        managed = bool(ac and ac.manages(s.device))
        actor = actor or schedule_actor(s)
        a = s.action
        if isinstance(a, (On, Adjust)) and not managed:
            return FireOutcome(s, at, key, "failed", f"{s.device} is not controlled by the AC encoder "
                               "(AC_ENCODER=off or no usable capture)")
        if isinstance(a, On):
            def send(): return self.devices.send_ac_state(s.device, a.state, actor, source=key)
            retry_any = True
        elif isinstance(a, Adjust):
            # Reads the last state and sends under the AC lock (never undoes a fresh Off)
            def send(): return self.devices.adjust_ac_state(s.device, dict(a.changes), actor, source=key)
            retry_any = True
        else:
            feature = "power_off" if isinstance(a, Off) else a.key
            if feature not in dev.features:
                return FireOutcome(s, at, key, "failed", f"{s.device} has no '{feature}' any more")
            def send(): return self.devices.run(s.device, feature, actor, source=key)
            retry_any = dev.features[feature].idempotent

        r, attempts = await send(), 1
        prev = _prev(r)
        for delay in (RETRY_DELAYS_S if retries else ()):
            if r.ok or r.error != "send_failed":
                break
            if r.data.get("maybe_delivered") and not retry_any:
                break     # a toggle may have run; sending again could undo it
            seq = r.data.get("seq")
            await self._sleep(delay)
            latest = self.devices.last.get(s.device)
            if latest and seq is not None and latest.seq != seq:
                return FireOutcome(s, at, key, "superseded", f"not retried: {latest.actor} sent something newer",
                                   r, attempts, prev)
            r, attempts = await send(), attempts + 1
        if r.ok:
            return FireOutcome(s, at, key, "ok", "", r, attempts, prev)
        if r.error == "ac_off":
            return FireOutcome(s, at, key, "skipped", "the AC is off", r, attempts, _prev(r))
        if r.error == "ac_state_unknown":
            return FireOutcome(s, at, key, "skipped", "the bot does not know the AC's state yet", r, attempts)
        return FireOutcome(s, at, key, "failed", r.message, r, attempts, prev)

    # ---- bookkeeping ---------------------------------------------------------------------

    async def _record(self, o: FireOutcome) -> bool:
        """Save `last_fired`; a timer is deleted once it has an outcome other than
        started. Returns False if it could not be saved."""
        today = self.now().date()

        def apply(schedules: dict[str, Schedule]) -> None:
            cur = schedules.get(o.schedule.id)
            if cur is None:
                return            # deleted meanwhile
            if cur.is_timer and o.result != "started":
                del schedules[cur.id]
                return
            skips = tm.live_skip_dates(cur, today)
            if o.result == "skipped":
                skips = tuple(d for d in skips if d != o.at.astimezone(self.tz).date())
            # rev is for user edits; the scheduler's own bookkeeping does not bump it
            schedules[cur.id] = replace(cur, skip_dates=skips,
                                        last_fired=LastFired(self.now(), o.key, o.result))

        try:
            await self.store.mutate(apply)
        except Exception:  # noqa: BLE001
            logger.exception("Could not save last_fired for %s", o.schedule.id)
            return False
        return True

    async def _notify(self, o: FireOutcome) -> None:
        if self.notify is None:
            return
        try:
            await self.notify(o)
        except Exception:  # noqa: BLE001
            logger.exception("Could not send the notice for %s", o.schedule.id)


def _prev(r: Result) -> AcState | None:
    return r.data.get("prev_state") if isinstance(r.data, dict) else None
