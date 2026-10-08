"""Durable JSON files and append-only JSONL logs.

No database: every change rewrites the whole JSON file atomically
(tmp file -> fsync -> rename -> fsync dir), keeping the previous good
version as `<name>.bak`. On a parse failure we fall back to the `.bak`
and report it, so a torn write after a power cut never loses everything.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import shutil
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class StoreError(Exception):
    pass


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json_atomic(path: Path, data: Any) -> None:
    """Serialize first, then replace `path` atomically, keeping a `.bak`."""
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if path.exists():
        # Only back up a file that still parses, so a bad file never overwrites a good .bak
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Not backing up unreadable %s", path)
        else:
            shutil.copyfile(path, path.with_name(path.name + ".bak"))
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def read_json(path: Path, default: Callable[[], Any]) -> tuple[Any, str | None]:
    """Load `path`; fall back to `.bak`, then to `default()` if neither exists.

    Returns (data, warning). Raises StoreError if the file exists but neither it
    nor its backup can be parsed -- refusing to start beats silently wiping data.
    """
    bak = path.with_name(path.name + ".bak")
    if not path.exists():
        if bak.exists():
            data = _parse(bak)
            return data, f"{path.name} missing, restored from {bak.name}"
        return default(), None
    try:
        return _parse(path), None
    except (OSError, ValueError) as e:
        if bak.exists():
            try:
                data = _parse(bak)
            except (OSError, ValueError) as e2:
                raise StoreError(f"{path} and {bak.name} both unreadable: {e}; {e2}") from e2
            return data, f"{path.name} unreadable ({e}), loaded {bak.name}"
        raise StoreError(f"{path} unreadable and no backup: {e}") from e


def _parse(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class JsonStore:
    """One JSON document kept in memory and saved on every change.

    Writers go through `update()`, which serializes changes with a lock, works on a
    copy, and only swaps the in-memory value after the file is safely on disk.
    """

    def __init__(self, path: Path, default: Callable[[], Any],
                 validate: Callable[[Any], None] | None = None):
        self.path = path
        self._default = default
        self._validate = validate
        self._lock = asyncio.Lock()
        self._data: Any = None
        self.load_warning: str | None = None

    def load(self) -> Any:
        data, warning = read_json(self.path, self._default)
        if self._validate:
            self._validate(data)
        self._data = data
        self.load_warning = warning
        if warning:
            logger.warning(warning)
        return data

    @property
    def data(self) -> Any:
        """Read-only view by convention; mutate only through update()."""
        if self._data is None:
            raise StoreError(f"{self.path} not loaded")
        return self._data

    async def update(self, fn: Callable[[Any], Any]) -> Any:
        """Apply `fn` to a deep copy, save, then commit. Returns fn's return value.

        If `fn` raises, nothing is saved and the in-memory value is unchanged.
        """
        async with self._lock:
            draft = copy.deepcopy(self.data)
            result = fn(draft)
            if self._validate:
                self._validate(draft)
            await asyncio.to_thread(write_json_atomic, self.path, draft)
            self._data = draft
            return result


class JsonlLog:
    """Append-only JSON-lines log split by month: `<dir>/<prefix>-YYYY-MM.jsonl`.

    Lines are flushed but not fsynced (SD card wear); losing the last few lines on
    a power cut is acceptable for logs.
    """

    def __init__(self, directory: Path, prefix: str, tz: str = "Asia/Singapore"):
        self.directory = directory
        self.prefix = prefix
        self.tz = ZoneInfo(tz)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def path_for(self, when: datetime) -> Path:
        return self.directory / f"{self.prefix}-{when.astimezone(self.tz):%Y-%m}.jsonl"

    def append(self, record: dict[str, Any], when: datetime | None = None) -> dict[str, Any]:
        when = when or self.now()
        rec = {"ts": when.isoformat(timespec="milliseconds"), **record}
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.path_for(when), "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            # Logging must never break a device action
            logger.error("Could not append to %s log: %s", self.prefix, e)
        return rec

    def read_month(self, year: int, month: int) -> Iterator[dict[str, Any]]:
        path = self.directory / f"{self.prefix}-{year:04d}-{month:02d}.jsonl"
        if not path.exists():
            return
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    # A torn last line after a power cut; skip it
                    logger.warning("Skipping bad line %d in %s", n, path.name)
