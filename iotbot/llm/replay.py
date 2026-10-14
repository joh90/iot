"""Replay log: every chat turn's full LLM requests and replies (Phase 4.5).

One file per day, `<log dir>/llm/llm-YYYY-MM-DD.jsonl`, deleted after KEEP_DAYS. Later
evals replay these against a new model or prompt. The system prompt and tool list are the
same for nearly every request, so each distinct one is written once per file as a
`prompt` line and requests point at it ("@prompt:<hash>"): about a third of the size.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

KEEP_DAYS = 90
NAME_RE = re.compile(r"^llm-(\d{4}-\d{2}-\d{2})\.jsonl$")


class ReplayLog:
    def __init__(self, directory: Path, tz: str, keep_days: int = KEEP_DAYS,
                 clock: Callable[[], float] = time.time):
        self.directory = directory
        self.tz = ZoneInfo(tz)
        self.keep_days = keep_days
        self._clock = clock
        self._seen: tuple[date, set[str]] | None = None    # prompt hashes already in today's file
        self._pruned: date | None = None
        self._lock = asyncio.Lock()

    async def write(self, record: Callable[[], dict[str, Any]], exchanges: list[dict[str, Any]]) -> None:
        """Never raises: the replay log must not cost a reply. `record` is built in here too."""
        try:
            async with self._lock:                         # one writer: the prompt-dedupe set stays right
                await asyncio.shield(asyncio.to_thread(self._write, record(), exchanges))
        except Exception:  # noqa: BLE001
            logger.exception("Could not write the LLM replay log")

    def _write(self, record: dict[str, Any], exchanges: list[dict[str, Any]]) -> None:
        now = datetime.fromtimestamp(self._clock(), self.tz)
        today = now.date()
        if self._seen is None or self._seen[0] != today:
            self._seen = (today, set())
        self.directory.mkdir(parents=True, exist_ok=True)
        lines: list[dict[str, Any]] = []
        new: set[str] = set()
        slim = []
        for ex in exchanges:
            req = ex.get("request") or {}
            msgs = req.get("messages") or []
            system = msgs[0]["content"] if msgs and msgs[0].get("role") == "system" else None
            tools = req.get("tools")
            key = hashlib.sha256(json.dumps([system, tools], sort_keys=True).encode()).hexdigest()[:12]
            if key not in self._seen[1] and key not in new:
                new.add(key)
                lines.append({"kind": "prompt", "hash": key, "system": system, "tools": tools})
            ref = f"@prompt:{key}"
            # Copy only what changes: the caller's exchange stays as it was
            slim_req = {**req, "messages": ([{**msgs[0], "content": ref}, *msgs[1:]] if system is not None else msgs)}
            if tools is not None:
                slim_req["tools"] = ref
            slim.append({**ex, "request": slim_req})
        lines.append({"kind": "turn", "ts": now.isoformat(timespec="milliseconds"), **record, "exchanges": slim})
        with open(self.directory / f"llm-{today:%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
            f.writelines(json.dumps(x, ensure_ascii=False, separators=(",", ":"), default=str) + "\n" for x in lines)
        self._seen[1].update(new)              # only once written: a failed write repeats its prompts
        if self._pruned != today:
            self._pruned = today
            self._prune(today)

    def _prune(self, today: date) -> None:
        for p in self.directory.iterdir():
            m = NAME_RE.match(p.name)
            if not m:
                continue                                    # never touch anything else in the folder
            try:
                age = (today - date.fromisoformat(m.group(1))).days
            except ValueError:
                continue
            if age > self.keep_days:
                try:
                    p.unlink()
                except OSError as e:
                    logger.warning("Could not delete old replay log %s: %s", p.name, e)
