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


def write_json_atomic(path: Path, data: Any, validate: Callable[[Any], None] | None = None) -> None:
    """Serialize first, then replace `path` atomically, keeping a `.bak`.

    The current file only becomes the `.bak` if it parses and passes `validate`,
    so a damaged or invalid file never overwrites a good backup. Validators
    must raise TypeError or ValueError, as for read_json.
    """
    text = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if validate:
                validate(current)
        except (OSError, ValueError, TypeError) as e:
            logger.warning("Not backing up unusable %s: %s", path, e)
        else:
            # Hard-link the current (already durable) inode as the backup: atomic, no copy.
            # os.replace below swaps in a new inode, so the link keeps the old content.
            bak_tmp = path.with_name(path.name + ".bak.tmp")
            bak_tmp.unlink(missing_ok=True)
            try:
                os.link(path, bak_tmp)
            except OSError:
                shutil.copyfile(path, bak_tmp)  # filesystems without hard links
                with open(bak_tmp, "rb") as f:
                    os.fsync(f.fileno())
            os.replace(bak_tmp, path.with_name(path.name + ".bak"))
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def read_json(path: Path, default: Callable[[], Any],
              validate: Callable[[Any], None] | None = None) -> tuple[Any, str | None]:
    """Load `path`; fall back to `.bak`, then to `default()` if neither exists.

    A file that parses but fails `validate` counts as unreadable. Returns
    (data, warning). Raises StoreError if a file exists but neither it nor its
    backup is usable -- refusing to start beats silently wiping data.
    """
    bak = path.with_name(path.name + ".bak")

    def load(p: Path) -> Any:
        data = _parse(p)
        if validate:
            validate(data)
        return data

    if not path.exists():
        if not bak.exists():
            data = default()
            if validate:
                validate(data)
            return data, None
        try:
            return load(bak), f"{path.name} missing, restored from {bak.name}"
        except (OSError, ValueError, TypeError) as e:
            raise StoreError(f"{path} missing and {bak.name} unusable: {e}") from e
    try:
        return load(path), None
    except (OSError, ValueError, TypeError) as e:
        if bak.exists():
            try:
                data = load(bak)
            except (OSError, ValueError, TypeError) as e2:
                raise StoreError(f"{path} and {bak.name} both unusable: {e}; {e2}") from e2
            return data, f"{path.name} unusable ({e}), loaded {bak.name}"
        raise StoreError(f"{path} unusable and no backup: {e}") from e


def _parse(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


_UNSET: Any = object()


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
        self._data: Any = _UNSET
        self.load_warning: str | None = None

    def load(self) -> Any:
        data, warning = read_json(self.path, self._default, self._validate)
        self._data = data
        self.load_warning = warning
        if warning:
            logger.warning(warning)
        return data

    def use_default(self) -> Any:
        """Start from `default()` without reading the file; the next save replaces it.

        For caches only: data files must refuse to start instead (see read_json).
        """
        self._data = self._default()
        self.load_warning = None
        return self._data

    @property
    def data(self) -> Any:
        """Read-only view by convention; mutate only through update()."""
        if self._data is _UNSET:
            raise StoreError(f"{self.path} not loaded")
        return self._data

    async def update(self, fn: Callable[[Any], Any]) -> Any:
        """Apply `fn` to a deep copy, save, then commit. Returns fn's return value.

        If `fn` raises, nothing is saved and the in-memory value is unchanged.
        Cancelling the caller does not cancel the write: memory is committed
        whenever the file was written, so disk and memory never diverge.
        Do not keep references into the returned value; copy what you need.
        """
        async with self._lock:
            draft = copy.deepcopy(self.data)
            result = fn(draft)
            if self._validate:
                self._validate(draft)
            write = asyncio.ensure_future(asyncio.to_thread(write_json_atomic, self.path, draft,
                                                          self._validate))
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                try:
                    await write
                except Exception:
                    raise asyncio.CancelledError() from None
                self._data = draft
                raise
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
        """Blocking append. From async code prefer `await log.append_async(...)`."""
        when = (when or self.now()).astimezone(self.tz)
        rec = {**record, "ts": when.isoformat(timespec="milliseconds")}
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.path_for(when)
            with open(path, "a+b") as f:
                # Start on a fresh line if a power cut left a torn last line
                if f.tell() > 0:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        f.write(b"\n")
                f.write(line.encode("utf-8"))
        except OSError as e:
            # Logging must never break a device action
            logger.error("Could not append to %s log: %s", self.prefix, e)
        return rec

    async def append_async(self, record: dict[str, Any], when: datetime | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self.append, record, when)

    def read_month(self, year: int, month: int) -> Iterator[dict[str, Any]]:
        path = self.directory / f"{self.prefix}-{year:04d}-{month:02d}.jsonl"
        if not path.exists():
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    # A torn last line after a power cut; skip it
                    logger.warning("Skipping bad line %d in %s", n, path.name)
