"""Turns: one log line per interaction (Phase 6).

A turn is one Telegram update (slash command, button tap, text) or one schedule fire.
The current turn lives in a contextvar, so the device service can add its send time
and outcome without every handler passing it along. Lines go to `turns-YYYY-MM.jsonl`
next to the other logs and are never deleted (raw text is kept: it becomes the LLM
eval set later). The chat fields (llm_ms, tokens, model, ...) are filled by the LLM layer;
its full requests go to the replay log (llm/replay.py).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any

from iotbot.store import JsonlLog

logger = logging.getLogger(__name__)

MAX_RAW = 1000
_current: ContextVar[Turn | None] = ContextVar("turn", default=None)


@dataclass(slots=True)
class Turn:
    surface: str                       # slash | button | text | scheduler | other
    user_id: int | None = None
    name: str | None = None
    allowed: bool | None = None
    raw: str | None = None             # message text or button data, as sent
    intent: str | None = None          # e.g. "/on", "kb:q", "schedule:off"
    # ok | error | denied | dropped_offline | cancelled | a Result error code (send_failed, ac_off, ...);
    # scheduler turns: ok | failed | skipped | superseded, with the code or reason in `error`
    outcome: str | None = None
    error: str | None = None
    update_id: int | None = None
    request_id: str | None = None
    lag_s: float | None = None         # Telegram message date -> received (whole seconds)
    total_ms: int | None = None        # inside the bot
    ir_ms: int = 0                     # summed over sends
    sends: int = 0
    attempts: int | None = None        # scheduler: tries incl. retries (total_ms includes the waits)
    maybe_delivered: bool = False
    devices: list[str] = field(default_factory=list)
    llm_ms: int | None = None
    tokens: int | None = None
    # Chat turns (Phase 4): the model, its tool calls ("name!" = refused) and rounds;
    # card = shown on the chat turn, or do | x | undo | retry on the tap that joins it by card_id
    model: str | None = None
    tool_calls: list[str] | None = None
    llm_rounds: int | None = None
    card: str | None = None
    card_id: str | None = None

    def record(self) -> dict[str, Any]:
        # Empty fields are left out: these files are kept forever
        rec = {k: v for k, v in asdict(self).items() if v is not None}
        if rec.get("raw") and len(rec["raw"]) > MAX_RAW:
            rec["raw"] = rec["raw"][:MAX_RAW] + "..."
        return rec


def current() -> Turn | None:
    return _current.get()


def note(**fields: Any) -> None:
    """Set fields on the current turn, if any (no-op outside a turn)."""
    t = _current.get()
    if t is not None:
        for k, v in fields.items():
            setattr(t, k, v)


def note_send(device: str, ms: int, ok: bool, maybe: bool) -> None:
    """Called by the device service after every send attempt."""
    t = _current.get()
    if t is None:
        return
    t.sends += 1
    t.ir_ms += ms
    if device not in t.devices:
        t.devices.append(device)
    if not ok and maybe:
        t.maybe_delivered = True


def note_result(ok: bool, error: str) -> None:
    """A device call's Result: the last one sets the outcome (scheduler retries end on the final try)."""
    note(outcome="ok" if ok else (error or "failed"))


class Tracker:
    def __init__(self, log: JsonlLog, clock: Callable[[], float] = time.perf_counter):
        self.log = log
        self._clock = clock

    @asynccontextmanager
    async def turn(self, **fields: Any) -> AsyncIterator[Turn]:
        t = Turn(**fields)
        token = _current.set(t)
        started = self._clock()
        # Logged at its start: a chat turn that waited for a card sorts before what it caused
        began = self.log.now()
        cancelled = False
        try:
            yield t
        except asyncio.CancelledError:
            cancelled = True
            t.outcome = t.outcome or "cancelled"
            raise
        except Exception as e:
            t.outcome, t.error = "error", t.error or e.__class__.__name__
            raise
        finally:
            _current.reset(token)
            t.total_ms = round((self._clock() - started) * 1000)
            if t.outcome is None:
                t.outcome = "ok"
            if cancelled:
                self.log.append(t.record(), when=began)      # an await here would be cancelled too
            else:
                try:
                    await self.log.append_async(t.record(), when=began)
                except Exception:  # noqa: BLE001 -- a log line never breaks a reply
                    logger.exception("Could not write a turn")
