"""Who is home: paired phones, probed once a minute (PLAN "Phase 8 decisions").

A person is home while their phone answers; away once it has not answered for AWAY_S
(locked phones go quiet for a while). After a restart everyone is "unknown": nobody is
offered anything until their phone is seen; someone never seen since the restart never
counts toward "everyone is out". A round where the router itself does not answer is
skipped (the Pi's network is down, not everyone gone). Arrive/leave times go to
`presence_events` (for Phase 7 patterns; each person can delete theirs).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from iotbot.presence.net import ANSWERED, GONE, normal_mac
from iotbot.store import JsonlLog, JsonStore

logger = logging.getLogger(__name__)

INTERVAL_S = 60
AWAY_S = 15 * 60             # unseen this long = away
ARRIVE_AFTER_S = 30 * 60     # an arrival offer only after this long away (popping out for coffee is not one)
SWEEP_EVERY_S = 5 * 60       # look for a phone whose IP changed at most this often
PAIR_WATCH_S = 120
PAIR_SETTLE_S = 10           # after a candidate appears, watch a little longer for a second one
PAIR_GAP_S = 2


class Network(Protocol):
    async def probe(self, ip: str) -> str: ...            # net.ANSWERED | SILENT | GONE
    async def lan_ok(self) -> bool: ...
    async def arp(self) -> dict[str, str]: ...
    async def sweep(self) -> dict[str, str]: ...


@dataclass(slots=True)
class Person:
    state: str = "unknown"            # unknown | home | away
    last_seen: float | None = None
    away_since: float | None = None   # their last sighting, when they went away


def validate_phones(data: Any) -> None:
    if not isinstance(data, dict):
        raise TypeError("phones.json must be an object of {\"<user id>\": {...}}")
    for uid, ph in data.items():
        if not (isinstance(uid, str) and uid.isdecimal()) or not isinstance(ph, dict):
            raise TypeError(f"phones.json: bad entry {uid!r}")
        if normal_mac(str(ph.get("mac", ""))) != ph.get("mac") or not isinstance(ph.get("ip"), str):
            raise TypeError(f"phones.json: entry {uid} needs a 'mac' (aa:bb:..) and an 'ip'")


class Presence:
    def __init__(self, store: JsonStore, net: Network, events: JsonlLog, allowed: Callable[[int], bool],
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.store = store
        self.net = net
        self.events = events
        self.allowed = allowed           # a removed user's phone stops counting at once
        self._clock = clock
        self._sleep = sleep
        self.started = clock()
        self.people: dict[int, Person] = {}
        self._last_sweep = 0.0
        self._all_away_told = False      # one departure offer per everyone-out stretch
        self._lan_down = False
        self._pair_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        # Set by the offers layer
        self.on_arrive: Callable[[int], Awaitable[None]] | None = None
        self.on_back: Callable[[int], Awaitable[None]] | None = None
        self.on_all_away: Callable[[float], Awaitable[None]] | None = None
        self.on_round: Callable[[], Awaitable[None]] | None = None      # after every round (offer expiry)

    # ---- phones ----------------------------------------------------------------------------

    def phones(self) -> dict[int, dict[str, Any]]:
        return {int(uid): ph for uid, ph in self.store.data.items() if self.allowed(int(uid))}

    def phone(self, uid: int) -> dict[str, Any] | None:
        return self.phones().get(uid)

    def counts(self) -> tuple[int, int]:
        """(home, paired): counts only, never names (PLAN: visibility)."""
        phones = self.phones()
        return sum(1 for uid in phones if self.person(uid).state == "home"), len(phones)

    def person(self, uid: int) -> Person:
        return self.people.setdefault(uid, Person())

    def is_home(self, uid: int) -> bool:
        return self.person(uid).state == "home"

    async def pair(self, uid: int, mac: str, ip: str) -> None:
        now = self._clock()
        record = {"mac": mac, "ip": ip, "arrival": True, "departure": True,
                  "paired_at": datetime.fromtimestamp(now, self.events.tz).isoformat(timespec="seconds")}

        def apply(data: dict) -> None:
            for other, ph in list(data.items()):
                if ph.get("mac") == mac and other != str(uid):
                    del data[other]                 # one phone, one person
            data[str(uid)] = record

        await self.store.update(apply)
        self.people[uid] = Person("home", now)      # just seen: they are pairing it at home
        self._all_away_told = False
        if self.on_back:
            await self.on_back(uid)                 # someone is home: an open "everyone is out" closes

    async def unpair(self, uid: int) -> bool:
        def apply(data: dict) -> bool:
            return data.pop(str(uid), None) is not None

        gone = await self.store.update(apply)
        self.people.pop(uid, None)
        return bool(gone)

    async def set_pref(self, uid: int, key: str, on: bool) -> None:
        if key not in ("arrival", "departure"):
            raise ValueError(key)

        def apply(data: dict) -> None:
            if str(uid) in data:
                data[str(uid)][key] = on

        await self.store.update(apply)

    def wants(self, uid: int, key: str) -> bool:
        ph = self.phone(uid)
        return bool(ph and ph.get(key, True))

    # ---- rounds ----------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run_forever(), name="presence")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- one bad round must not end presence
                logger.exception("Presence round failed")
            try:
                if self.on_round:
                    await self.on_round()           # offer expiry, even after a failed round
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Presence offer expiry failed")
            await self._sleep(INTERVAL_S)

    async def tick(self) -> None:
        phones = self.phones()
        for uid in [u for u in self.people if u not in phones]:
            del self.people[uid]                    # unpaired or removed user
        if not phones:
            return
        if not await self.net.lan_ok():
            if not self._lan_down:
                logger.warning("Presence: the router does not answer; skipping rounds until it does")
            self._lan_down = True
            return
        if self._lan_down:
            # Back from an outage: nobody was watched meanwhile, so nobody went away or arrived
            logger.info("Presence: the router answers again")
            self._lan_down = False
            now = self._clock()
            self.started = now               # "unknown" people count from here, not from before the outage
            for uid in phones:
                p = self.person(uid)
                if p.state == "home":
                    p.last_seen = now
        home_before = {u for u in phones if self.person(u).state == "home"}
        seen = await self._look(phones)
        now = self._clock()
        arrived: list[tuple[int, float]] = []
        went_away = False
        for uid in phones:
            p = self.person(uid)
            if seen.get(uid):
                if p.state == "away":
                    away_for = now - (p.away_since or now)
                    await self._log(uid, "home", away_s=round(away_for))
                    arrived.append((uid, away_for))
                p.state, p.last_seen, p.away_since = "home", now, None
            else:
                ref = p.last_seen if p.last_seen is not None else self.started
                if p.state != "away" and now - ref >= AWAY_S:
                    if p.state == "home":
                        await self._log(uid, "away", since=datetime.fromtimestamp(ref, self.events.tz)
                                        .isoformat(timespec="seconds"))
                    p.state, p.away_since = "away", ref
                    went_away = True
        if any(self.person(u).state != "away" for u in phones):
            self._all_away_told = False
        for uid, away_for in arrived:
            if self.on_back:
                await self.on_back(uid)
            # Only for whoever comes home to an empty home: two arriving together both get one
            if away_for >= ARRIVE_AFTER_S and not (home_before - {uid}) and self.on_arrive:
                await self.on_arrive(uid)
        # Everyone away, at least one of them really seen leaving since the restart; "since" comes
        # only from those (someone never seen since the restart has been away at least that long)
        seen_leave = [self.person(u).away_since for u in phones if self.person(u).last_seen is not None]
        if (went_away and not self._all_away_told and seen_leave
                and all(self.person(u).state == "away" for u in phones)):
            self._all_away_told = True
            if self.on_all_away:
                await self.on_all_away(max(t or now for t in seen_leave))

    async def _look(self, phones: dict[int, dict[str, Any]]) -> dict[int, bool]:
        """One probe per phone; a phone whose IP now answers with another MAC (DHCP gave
        its address away) is not seen there, and a sweep looks for it elsewhere."""
        uids = list(phones)
        results = await asyncio.gather(*(self.net.probe(phones[u]["ip"]) for u in uids))
        arp = await self.net.arp()
        seen = {}
        for uid, result in zip(uids, results):
            owner = arp.get(phones[uid]["ip"])
            seen[uid] = result != GONE and (owner is None or owner == phones[uid]["mac"])
        now = self._clock()
        lost = [u for u in uids if not seen[u]]
        if lost and now - self._last_sweep >= SWEEP_EVERY_S:
            self._last_sweep = now
            found = await self.net.sweep()
            moved = {u: found[phones[u]["mac"]] for u in lost if phones[u]["mac"] in found}
            if moved:
                def apply(data: dict) -> None:
                    for u, ip in moved.items():
                        if str(u) in data:
                            data[str(u)]["ip"] = ip

                await self.store.update(apply)
                for u in moved:
                    seen[u] = True
        return seen

    async def _log(self, uid: int, event: str, **extra: Any) -> None:
        await self.events.append_async({"user_id": uid, "event": event, **extra})

    # ---- pairing ---------------------------------------------------------------------------

    async def watch_for_toggle(self, ready: Callable[[int], Awaitable[None]] | None = None,
                               seconds: float = PAIR_WATCH_S) -> list[tuple[str, str]]:
        """For /phone: sweep, then probe every unpaired device until one goes away and comes
        back (the person toggling Wi-Fi). Returns [(mac, ip)] of the devices that did."""
        if self._pair_lock.locked():
            raise RuntimeError("busy")
        async with self._pair_lock:
            taken = {ph["mac"] for ph in self.store.data.values()}
            ips = {mac: ip for mac, ip in (await self.net.sweep()).items() if mac not in taken}
            if ready:
                await ready(len(ips))
            history = {mac: [True] for mac in ips}
            deadline = self._clock() + seconds
            settle_at = None
            while self._clock() < deadline and ips:
                macs = list(ips)
                results = await asyncio.gather(*(self.net.probe(ips[m]) for m in macs))
                arp = await self.net.arp()
                for mac, result in zip(macs, results):
                    # Only a real answer counts here: a phone switched off a few seconds ago
                    # still times out (SILENT) rather than failing, see net.py
                    there = result == ANSWERED and arp.get(ips[mac], mac) == mac
                    if history[mac][-1] != there:
                        history[mac].append(there)
                done = [m for m, h in history.items() if len(h) >= 3]      # there, gone, back
                if done and settle_at is None:
                    settle_at = self._clock() + PAIR_SETTLE_S
                if settle_at is not None and self._clock() >= settle_at:
                    break
                await self._sleep(PAIR_GAP_S)
            return [(m, ips[m]) for m, h in history.items() if len(h) >= 3]

    # ---- history ---------------------------------------------------------------------------

    async def forget_history(self, uid: int) -> int:
        """Delete this person's arrive/leave lines from every presence_events file."""
        return await asyncio.to_thread(self._forget, uid)

    def _forget(self, uid: int) -> int:
        removed = 0
        directory = self.events.directory
        if not directory.exists():
            return 0
        for path in sorted(directory.glob(f"{self.events.prefix}-*.jsonl")):
            keep = []
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        mine = json.loads(line).get("user_id") == uid
                    except (ValueError, AttributeError):
                        mine = False
                    if mine:
                        removed += 1
                    else:
                        keep.append(line)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(keep)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        return removed
