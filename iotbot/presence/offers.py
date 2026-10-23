"""Welcome-home and everyone-out offers (PLAN "Phase 8 decisions").

Telegram-free like schedule/notify.py: builds text + button rows and hands them to
`send`/`edit` coroutines the bot layer provides. Nothing is ever sent to an aircon
without a tap. Offers live in memory: after a restart their buttons say so.

- Arrival: to the person who got home after 30+ min away, only if nobody else paired is
  home and an aircon is off: a button per aircon that is off, [Both], [No thanks]. On
  uses the last state the bot sent to that aircon, powered on (room preset if none).
- Departure: once everyone paired is away and an aircon is on, to everyone paired who
  wants it (silent at night): [Turn off all], one per aircon, [Leave on]. The first tap
  wins; every copy is redrawn. Someone coming back closes it.
Presence text never names who is home (counts only); a tap result names the tapper.
"""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from iotbot.ac.service import AcService
from iotbot.ac.state import AcState
from iotbot.bot.callbacks import encode
from iotbot.devices.model import Registry
from iotbot.presence.tracker import Presence
from iotbot.result import Actor, Result
from iotbot.services.devices import DeviceService

logger = logging.getLogger(__name__)

NS = "pr"
ARRIVAL_TTL_S = 30 * 60
DEPARTURE_TTL_S = 2 * 3600
UNDO_S = 10 * 60
NIGHT = (23, 7)              # departure offers are silent from 23:00 to 07:00
MAX_OFFERS = 50

Rows = list[list[tuple[str, str]]]
Send = Callable[[int, str, Rows, bool], Awaitable[int | None]]     # -> message id
Edit = Callable[[int, int, str, Rows], Awaitable[None]]


def h(text: object) -> str:
    return html.escape(str(text), quote=False)


@dataclass(slots=True)
class Offer:
    id: str
    kind: str                                  # arrival | departure
    at: float                                  # arrived / everyone out since
    expires: float
    copies: dict[int, int] = field(default_factory=dict)      # user id -> message id
    status: str = "open"                       # open | closed
    # What taps did, one line each: (tapper id, tapper name, text with {who})
    done: list[tuple[int, str, str]] = field(default_factory=list)
    closed_why: str = ""
    # device -> (seq of our send, state before it or None if the bot never sent one)
    undo: dict[str, tuple[int | None, AcState | None]] = field(default_factory=dict)
    undo_until: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Offers:
    def __init__(self, presence: Presence, devices: DeviceService, ac: AcService, registry: Registry,
                 name_of: Callable[[int], str], tz: str, clock: Callable[[], float] = time.time):
        self.presence = presence
        self.devices = devices
        self.ac = ac
        self.registry = registry
        self.name_of = name_of
        self.tz = ZoneInfo(tz)
        self._clock = clock
        self.send: Send | None = None
        self.edit: Edit | None = None
        self.offers: dict[str, Offer] = {}
        presence.on_arrive = self.arrive
        presence.on_back = self.back
        presence.on_all_away = self.all_away
        presence.on_round = self.expire

    # ---- aircons ---------------------------------------------------------------------------

    def aircons(self) -> list[str]:
        return [d for d in self.ac.presets if d in self.registry.devices]

    def is_on(self, device: str) -> bool:
        last = self.ac.last(device)
        return bool(last and last.state.power)

    def label(self, device: str) -> str:
        """'Bedroom' (its room), or the device id when two aircons share a room."""
        room = self.registry.devices[device].room
        same = [d for d in self.aircons() if self.registry.devices[d].room == room]
        return room.replace("_", " ").replace("-", " ").title() if len(same) == 1 else device

    def _hhmm(self, t: float) -> str:
        return f"{datetime.fromtimestamp(t, self.tz):%H:%M}"

    # ---- presence events -------------------------------------------------------------------

    async def arrive(self, uid: int) -> None:
        """The tracker calls this only for someone coming home to an empty home."""
        if not self.presence.wants(uid, "arrival"):
            return
        if not any(not self.is_on(d) for d in self.aircons()):
            return
        for o in self._open("arrival", uid):
            await self._close(o, "")
        now = self._clock()
        o = self._new("arrival", now, ARRIVAL_TTL_S)
        await self._deliver(o, [uid], silent=False)

    async def all_away(self, since: float) -> None:
        if not any(self.is_on(d) for d in self.aircons()):
            return
        who = [u for u in self.presence.phones() if self.presence.wants(u, "departure")]
        if not who:
            return
        o = self._new("departure", since, DEPARTURE_TTL_S)
        hour = datetime.fromtimestamp(self._clock(), self.tz).hour
        await self._deliver(o, who, silent=hour >= NIGHT[0] or hour < NIGHT[1])

    async def back(self, uid: int) -> None:
        for o in self._open("departure"):
            await self._close(o, f"Someone is back ({self._hhmm(self._clock())}). Offer closed.")

    async def expire(self) -> None:
        """Called every presence round: close offers past their time, drop old ones."""
        now = self._clock()
        for o in [o for o in self.offers.values() if o.status == "open" and o.expires <= now]:
            await self._close(o, "Offer expired.")
        for o in [o for o in self.offers.values() if o.undo and o.undo_until <= now]:
            o.undo.clear()
            await self._redraw(o)
        old = [k for k, o in self.offers.items() if o.status == "closed" and now - o.expires > 86400]
        for k in old:
            del self.offers[k]

    def _open(self, kind: str, uid: int | None = None) -> list[Offer]:
        return [o for o in self.offers.values()
                if o.kind == kind and o.status == "open" and (uid is None or uid in o.copies)]

    def _new(self, kind: str, at: float, ttl: float) -> Offer:
        while len(self.offers) >= MAX_OFFERS:
            del self.offers[next(iter(self.offers))]
        oid = secrets.token_hex(3)
        while oid in self.offers:
            oid = secrets.token_hex(3)
        o = Offer(oid, kind, at, self._clock() + ttl)
        self.offers[oid] = o
        return o

    async def _deliver(self, o: Offer, users: list[int], silent: bool) -> None:
        if self.send is None:
            return
        for uid in users:
            text, rows = self.render(o, uid)
            try:
                mid = await self.send(uid, text, rows, silent)
            except Exception:  # noqa: BLE001 -- one person's chat must not stop the others
                logger.exception("Could not send a presence offer to %s", uid)
                continue
            if mid is not None:
                o.copies[uid] = mid

    async def _close(self, o: Offer, why: str) -> None:
        o.status, o.closed_why = "closed", why
        await self._redraw(o)

    async def _redraw(self, o: Offer) -> None:
        if self.edit is None:
            return
        for uid, mid in o.copies.items():
            text, rows = self.render(o, uid)
            try:
                await self.edit(uid, mid, text, rows)
            except Exception:  # noqa: BLE001 -- best effort: an old message may be gone
                logger.info("Could not redraw presence offer %s for %s", o.id, uid)

    # ---- screens ---------------------------------------------------------------------------

    def render(self, o: Offer, uid: int) -> tuple[str, Rows]:
        if o.kind == "arrival":
            lines = [f"Welcome home ({self._hhmm(o.at)})."]
            targets = [d for d in self.aircons() if not self.is_on(d)]
        else:
            lines = [f"Everyone is out (since {self._hhmm(o.at)})."]
            targets = [d for d in self.aircons() if self.is_on(d)]
        lines += [text.replace("{who}", "you" if who == uid else h(name)) for who, name, text in o.done]
        rows: Rows = []
        if o.status == "open":
            if o.kind == "arrival":
                if not o.done:
                    lines.append(self._off_line(targets))
                buttons = [(self.label(d) + (" too" if o.done else ""), encode(NS, "on", o.id, d)) for d in targets]
                if len(targets) >= 2:
                    buttons.append(("Both" if len(targets) == 2 else "All", encode(NS, "on", o.id, "all")))
                rows = [buttons] if buttons else []
                if not o.done:
                    rows.append([("No thanks", encode(NS, "no", o.id))])
            else:
                if targets:
                    lines.append("Still on: " + ", ".join(self._since(d) for d in targets))
                    if len(targets) >= 2:
                        rows.append([("Turn off all", encode(NS, "off", o.id, "all"))])
                    rows.append([(f"{self.label(d)} off", encode(NS, "off", o.id, d)) for d in targets])
                    rows.append([("Leave on", encode(NS, "no", o.id))])
        elif o.closed_why:
            lines.append(h(o.closed_why))
        if o.undo and o.undo_until > self._clock():
            rows.insert(0, [("Undo", encode(NS, "undo", o.id))])
        return "\n".join(lines), rows

    def _off_line(self, off: list[str]) -> str:
        if len(off) == len(self.aircons()) and len(off) >= 2:
            return "Both aircons are off." if len(off) == 2 else "All aircons are off."
        return " and ".join(h(self.label(d)) for d in off) + (" aircon is off." if len(off) == 1 else " aircons are off.")

    def _since(self, device: str) -> str:
        last = self.ac.last(device)
        since = last.on_since if last else None
        return h(self.label(device)) + (f" (since {self._hhmm(since)})" if since else "")

    # ---- taps ------------------------------------------------------------------------------

    async def tap(self, actor: Actor, op: str, args: list[str]) -> Result:
        o = self.offers.get(args[0]) if args else None
        if o is None:
            return Result.fail("expired", "This offer is no longer active.")
        if actor.user_id not in o.copies:
            return Result.fail("not_yours", "This offer was sent to someone else.")
        async with o.lock:                      # a double tap or two people at once: one wins
            if op == "undo":
                r = await self._undo(o, actor)
            elif o.status != "open":
                r = Result.fail("closed", "This offer is closed.")
            elif op == "no":
                self._did(o, actor, ("Left off" if o.kind == "arrival" else "Left on")
                          + f" (by {{who}}, {self._hhmm(self._clock())}).")
                o.status = "closed"
                r = Result.success("OK, nothing changed.")
            elif op in ("on", "off") and len(args) > 1 and op == ("on" if o.kind == "arrival" else "off"):
                r = await self._switch(o, actor, args[1])
            else:
                r = Result.fail("invalid", "This button is no longer active.")
        await self._redraw(o)
        return r

    async def _switch(self, o: Offer, actor: Actor, target: str) -> Result:
        turn_on = o.kind == "arrival"
        pool = [d for d in self.aircons() if self.is_on(d) != turn_on]
        devices = pool if target == "all" else [d for d in pool if d == target]
        if not devices:
            return Result.fail("already", "Already " + ("on" if turn_on else "off") + "; nothing sent.")
        done, failed = [], []
        for d in devices:
            last = self.ac.last(d)
            if turn_on:
                state = (last.state.with_changes(power=True, powerful=False) if last
                         else self.ac.state_for(d, "power_on"))
            else:
                state = self.ac.state_for(d, "power_off")
            r = await self.devices.send_ac_state(d, state, actor, source=f"presence:{o.id}")
            if r.ok:
                seq = r.data.get("seq") if isinstance(r.data, dict) else None
                o.undo.setdefault(d, (seq, last.state if last else None))
                o.undo[d] = (seq, o.undo[d][1])          # newest send, oldest earlier state
                done.append(d)
            else:
                failed.append(f"{self.label(d)}: {r.message}")
        if done:
            when = self._hhmm(self._clock())
            if turn_on:
                for d in done:
                    self._did(o, actor, f"{h(self.label(d))} on, {h(self.ac.last(d).state.describe())} "
                                        f"(by {{who}}, {when})")
            else:
                what = "Turned off all" if target == "all" and not failed else \
                    " and ".join(h(self.label(d)) for d in done) + " off"
                self._did(o, actor, f"{what} (by {{who}}, {when}).")
            o.undo_until = self._clock() + UNDO_S
            left = [d for d in self.aircons() if self.is_on(d) != turn_on]
            if not left:
                o.status = "closed"
        if failed:
            return Result.fail("send_failed", "Not sent: " + "; ".join(failed))
        return Result.success("Done.")

    @staticmethod
    def _did(o: Offer, actor: Actor, text: str) -> None:
        """`text` is HTML with "{who}": "you" on the tapper's copy, their name on the others."""
        o.done.append((actor.user_id, actor.name, text))

    async def _undo(self, o: Offer, actor: Actor) -> Result:
        if not o.undo or o.undo_until <= self._clock():
            return Result.fail("expired", "Undo is only possible for 10 minutes.")
        problems = []
        for d, (seq, before) in list(o.undo.items()):
            state = before if before is not None else self.ac.state_for(d, "power_off")
            r = await self.devices.send_ac_state(d, state, actor, source=f"presence-undo:{o.id}",
                                                 expect_seq=seq)
            if r.ok:
                del o.undo[d]
            else:
                problems.append(f"{self.label(d)}: {r.message}")
        if not problems:
            self._did(o, actor, f"Undone (by {{who}}, {self._hhmm(self._clock())}).")
            o.status = "closed"
            return Result.success("Undone.")
        return Result.fail("undo_failed", "Not undone: " + "; ".join(problems))
