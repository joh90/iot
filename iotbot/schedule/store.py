"""`state/schedules.json`: every schedule, kept in memory, saved on every change.

A schedule that fails to parse never stops the bot: it stays in the file
untouched, is listed as broken with the reason, and never fires. A file that
cannot be read at all (nor its `.bak`) is moved aside to
`schedules.json.broken-<time>` and the bot starts with no schedules, so the AC
buttons keep working and nothing is deleted.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from iotbot.schedule.model import Schedule, ScheduleError, empty_file, parse_schedule, validate_file
from iotbot.store import JsonStore, StoreError

logger = logging.getLogger(__name__)


class ScheduleStore:
    def __init__(self, path: Path):
        self.path = path
        self._store = JsonStore(path, empty_file, validate=validate_file)
        self._parsed: dict[str, Schedule] = {}
        self.broken: dict[str, str] = {}          # id -> reason
        self.load_warning: str | None = None

    def load(self) -> None:
        try:
            self._store.load()
            self.load_warning = self._store.load_warning
        except StoreError as e:
            # Move the file AND its backup aside: the next saves would otherwise
            # turn the unreadable main file into the .bak and overwrite the old .bak
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            moves = []
            for src in (self.path, self.path.with_name(self.path.name + ".bak")):
                if not src.exists():
                    continue
                dst = src.with_name(f"{src.name}.broken-{stamp}")
                try:
                    os.replace(src, dst)
                    moves.append(f"moved {src.name} to {dst.name}")
                except OSError as e2:
                    moves.append(f"could not move {src.name} aside ({e2})")
            moved = "; ".join(moves) or "nothing to move"
            self._store.use_default()
            self.load_warning = f"{e}; {moved}; starting with no schedules"
            logger.error(self.load_warning)
        self._reparse()

    def _reparse(self) -> None:
        parsed, broken = {}, {}
        for key, raw in self._store.data["schedules"].items():
            try:
                s = parse_schedule(raw)
                if s.id != key:
                    raise ScheduleError(f"stored under '{key}' but its id is '{s.id}'")
            except ScheduleError as e:
                broken[key] = str(e)
                logger.warning("Schedule %s is broken: %s", key, e)
                continue
            parsed[key] = s
        self._parsed, self.broken = parsed, broken

    def all(self) -> dict[str, Schedule]:
        """Working schedules by id (broken ones excluded; see `broken`)."""
        return dict(self._parsed)

    def get(self, sid: str) -> Schedule | None:
        return self._parsed.get(sid)

    def ids(self) -> set[str]:
        """Every id in the file, broken ones included (for picking new ids)."""
        return set(self._store.data["schedules"])

    async def mutate(self, fn: Callable[[dict[str, Schedule]], Any]) -> Any:
        """Apply `fn` to the working schedules {id: Schedule}; it may add, replace or
        delete entries. Saved atomically, then committed. Returns fn's return value.

        Raises ScheduleError (and changes nothing) if `fn` raises, puts a schedule
        under the wrong id or a broken entry's id, or adds one that would not load
        back identically. Broken entries are never touched (see drop_broken).
        """
        def apply(data: dict) -> Any:
            # Built from the draft being saved, never from the in-memory cache, so a
            # previously cancelled write cannot make this one delete a schedule
            raw = data["schedules"]
            working, broken = {}, set()
            for sid, entry in raw.items():
                try:
                    s = parse_schedule(entry)
                    if s.id != sid:
                        raise ScheduleError("id mismatch")
                    working[sid] = s
                except ScheduleError:
                    broken.add(sid)
            result = fn(working)
            for sid in list(raw):
                if sid not in broken and sid not in working:
                    del raw[sid]
            for sid, s in working.items():
                if sid in broken:
                    raise ScheduleError(f"id {sid} belongs to a broken schedule")
                if s.id != sid:
                    raise ScheduleError(f"schedule {s.id} put under '{sid}'")
                entry = s.to_dict()
                if parse_schedule(entry) != s:
                    raise ScheduleError(f"schedule {sid} would not load back the same: {entry}")
                raw[sid] = entry
            data["saved_at"] = time.time()
            return result

        try:
            return await self._store.update(apply)
        finally:
            self._reparse()

    async def drop_broken(self, sid: str) -> bool:
        """Delete a broken entry from the file. Returns False if it is not broken."""
        if sid not in self.broken:
            return False

        def apply(data: dict) -> None:
            data["schedules"].pop(sid, None)
            data["saved_at"] = time.time()

        await self._store.update(apply)
        self._reparse()
        return True

    def latest_timestamp(self) -> float | None:
        """Newest time the bot saved (file `saved_at`, edits, fires), for the boot clock check.
        `saved_at` keeps it from going backwards when a fired timer is deleted."""
        stamps = [s.updated_at.timestamp() for s in self._parsed.values()]
        if (saved := self._store.data.get("saved_at")) is not None:
            stamps.append(float(saved))
        stamps += [s.last_fired.at.timestamp() for s in self._parsed.values() if s.last_fired]
        return max(stamps, default=None)
