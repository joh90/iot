"""Confirm cards for chat actions: [Do it] [Cancel], then [Retry failed] [Undo] (Phase 4.3).

Code draws every card; the model never says an action is done. Training wheels: every
chat action gets a card. Rules (PLAN "Phase 4 decisions"):
- a card lives CARD_TTL_S; the newest card per person wins (older ones become "Replaced");
- Do it refuses if a device or schedule changed since the card was drawn (the
  before -> after lines would be wrong);
- Undo lives UNDO_TTL_S after Do it and is refused per line if something newer happened.
In memory only: after a restart old card buttons answer "no longer active".
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from iotbot.result import Actor

logger = logging.getLogger(__name__)

CARD_TTL_S = 300
UNDO_TTL_S = 600
MAX_CARDS = 200                     # old cards are dropped beyond this (memory bound)


@dataclass(slots=True)
class Done:
    ok: bool
    text: str                                                   # "ok", or why it failed
    undo: Callable[[Actor], Awaitable[Done]] | None = None      # set when this line can be undone


@dataclass(slots=True)
class Prepared:
    """One card line, built from the state when the card was drawn."""
    name: str                                         # "bedroom-aircon", "schedule Bed time"
    line: str                                         # "ON cool 24C fan auto vane auto -> OFF"
    run: Callable[[Actor], Awaitable[Done]]
    # Changes whenever THIS device or schedule changes (per item, never a global counter:
    # Retry compares it after Do, when the card's other lines have changed their own)
    version: Callable[[], Any]
    seen: Any = None                                  # version() when drawn

    def __post_init__(self) -> None:
        self.seen = self.version()

    def changed(self) -> bool:
        return self.version() != self.seen


@dataclass(slots=True)
class Card:
    id: str
    user_id: int
    items: list[Prepared]
    expires: float
    status: str = "pending"          # pending | running | done | cancelled | replaced | expired | undone
    results: list[Done | None] = field(default_factory=list)
    undo_until: float = 0.0
    chat_id: int | None = None
    message_id: int | None = None
    context: dict[str, Any] | None = None             # the chat-context message to update


INTERRUPTED = Done(False, "interrupted, may not have been sent")


class CardError(Exception):
    """A tap that cannot run; the message is shown to the person."""


class Cards:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self.cards: dict[str, Card] = {}

    def new(self, user_id: int, items: list[Prepared]) -> tuple[Card, list[Card]]:
        """A new pending card; returns it and the person's older pending cards, now replaced."""
        replaced = [c for c in self.cards.values() if c.user_id == user_id and c.status == "pending"]
        for c in replaced:
            c.status = "replaced"
        cid = secrets.token_hex(4)
        while cid in self.cards:
            cid = secrets.token_hex(4)
        card = Card(cid, user_id, items, self._clock() + CARD_TTL_S)
        self.cards[cid] = card
        self._prune()
        return card, replaced

    def _prune(self) -> None:
        now = self._clock()
        for cid in [cid for cid, c in self.cards.items() if self._dead(c, now)]:
            del self.cards[cid]
        # Over the cap (only with many people): drop the oldest that are not running
        for cid in [cid for cid, c in self.cards.items() if c.status != "running"][:len(self.cards) - MAX_CARDS]:
            del self.cards[cid]

    @staticmethod
    def _dead(c: Card, now: float) -> bool:
        if c.status == "pending":
            return now > c.expires + UNDO_TTL_S
        return c.status != "running" and now > max(c.expires, c.undo_until)

    def get(self, cid: str, user_id: int) -> Card:
        c = self.cards.get(cid)
        if c is None or c.user_id != user_id:
            raise CardError("This button is no longer active.")
        return c

    def cancel(self, c: Card) -> None:
        if c.status != "pending":
            raise CardError(_not_pending(c))
        c.status = "cancelled"

    # Each tap is begin (sync: check and claim the card, raises CardError) then run (async).
    # The caller answers the tap between the two, so a refusal is always the first answer.

    def begin(self, c: Card, op: str) -> None:
        if op == "do":
            if c.status == "pending" and self._clock() > c.expires:
                c.status = "expired"
            if c.status != "pending":
                raise CardError(_not_pending(c))
            changed = [p.name for p in c.items if p.changed()]
            if changed:
                c.status = "expired"
                raise CardError(f"{', '.join(changed)} changed since this card was drawn, so nothing was "
                                "done. Ask again.")
        elif op in ("retry", "undo"):
            if c.status == "running":
                raise CardError("Still working on it.")
            if c.status != "done":
                raise CardError(_not_pending(c) if c.status != "pending" else "This button is no longer active.")
            if self._clock() > c.undo_until:
                raise CardError("Too late: undo and retry last 10 min.")
            if op == "retry" and not self._failed(c):
                raise CardError("Nothing failed.")
            if op == "undo" and not self._undoable(c):
                raise CardError("Nothing here can be undone.")
        else:
            raise CardError("This button is no longer active.")
        c.status = "running"                       # a double tap is refused above from here on

    async def run(self, c: Card, op: str, actor: Actor) -> list[Done | None]:
        """After begin(). Returns the per-line results of this tap."""
        try:
            if op == "do":
                return await self._do(c, actor)
            if op == "retry":
                return await self._retry(c, actor)
            return await self._undo(c, actor)
        finally:
            if c.status == "running":
                c.status = "done"

    async def _do(self, c: Card, actor: Actor) -> list[Done | None]:
        c.results = [INTERRUPTED] * len(c.items)          # filled line by line: a cancel keeps what ran
        c.undo_until = self._clock() + UNDO_TTL_S
        for i, p in enumerate(c.items):
            c.results[i] = await _run(p.run, actor)
            p.seen = p.version()                           # Retry compares against after-Do
        c.undo_until = self._clock() + UNDO_TTL_S
        return list(c.results)

    async def _retry(self, c: Card, actor: Actor) -> list[Done | None]:
        for i in self._failed(c):
            p = c.items[i]
            if p.changed():
                c.results[i] = Done(False, "changed since, not sent again")
                continue
            c.results[i] = await _run(p.run, actor)
            p.seen = p.version()
        return list(c.results)

    async def _undo(self, c: Card, actor: Actor) -> list[Done | None]:
        """Undo every line that worked. A line that undid is not undone twice; if any line is
        left undoable (its undo failed), the card stays 'done' so Undo can be tried again."""
        out: list[Done | None] = [None] * len(c.items)
        for i, r in enumerate(c.results):
            if r and r.ok and r.undo:
                u = await _run(r.undo, actor)
                out[i] = u
                if u.ok:
                    r.undo = None
        c.status = "done" if self._undoable(c) else "undone"
        return out

    @staticmethod
    def _failed(c: Card) -> list[int]:
        return [i for i, r in enumerate(c.results) if r is not None and not r.ok]

    @staticmethod
    def _undoable(c: Card) -> bool:
        return any(r and r.ok and r.undo for r in c.results)


async def _run(fn: Callable[[Actor], Awaitable[Done]], actor: Actor) -> Done:
    try:
        return await fn(actor)
    except Exception as e:  # noqa: BLE001 -- one broken line must not stop the others
        logger.exception("Card line failed")
        return Done(False, f"error ({e.__class__.__name__})")


def _not_pending(c: Card) -> str:
    return {
        "running": "Still working on it.",
        "done": "Already done.",
        "undone": "Already done and undone.",
        "cancelled": "This was cancelled.",
        "replaced": "Replaced by your newer request; nothing was done.",
        "expired": "This card expired; nothing was done. Ask again.",
    }.get(c.status, "This button is no longer active.")
