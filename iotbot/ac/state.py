"""Aircon state: the full setting the bot last sent to each AC.

AC remotes send the whole state on every press, so the bot builds each frame from
a complete `AcState` and remembers the last one it sent. That memory is a cache of
what the bot asked for, not what the AC is doing (the physical remote can change it).

Phase 3 scope: cool mode only. Fan values follow the Mitsubishi 144-bit protocol,
which has 4 speeds plus quiet (there is no speed 5).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from iotbot.store import JsonStore, StoreError

logger = logging.getLogger(__name__)

MODES = ("cool",)
TEMP_MIN, TEMP_MAX = 16, 31
FANS: tuple[int | str, ...] = ("auto", 1, 2, 3, 4, "quiet")
VANES: tuple[int | str, ...] = ("auto", 1, 2, 3, 4, 5, "swing")


class AcStateError(ValueError):
    pass


def _choice(name: str, value: Any, allowed: tuple[Any, ...]) -> None:
    # Match type too: True == 1 and 3.0 == 3 would otherwise pass as fan 1 / fan 3
    if not any(type(value) is type(a) and value == a for a in allowed):
        raise AcStateError(f"{name} must be one of {', '.join(map(str, allowed))}, got {value!r}")


@dataclass(frozen=True, slots=True)
class AcState:
    power: bool
    temp: int
    fan: int | str = "auto"
    vane: int | str = "auto"
    mode: str = "cool"
    powerful: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.power, bool):
            raise AcStateError(f"power must be true or false, got {self.power!r}")
        if not isinstance(self.powerful, bool):
            raise AcStateError(f"powerful must be true or false, got {self.powerful!r}")
        if isinstance(self.temp, bool) or not isinstance(self.temp, int) \
                or not TEMP_MIN <= self.temp <= TEMP_MAX:
            raise AcStateError(f"temp must be a whole number {TEMP_MIN}-{TEMP_MAX}, got {self.temp!r}")
        _choice("fan", self.fan, FANS)
        _choice("vane", self.vane, VANES)
        _choice("mode", self.mode, MODES)

    def with_changes(self, **changes: Any) -> AcState:
        """A copy with `changes` applied. Raises AcStateError on bad values or unknown keys."""
        try:
            return replace(self, **changes)
        except TypeError as e:
            raise AcStateError(f"unknown AC setting: {e}") from None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> AcState:
        if not isinstance(data, dict):
            raise AcStateError(f"AC state must be an object, got {type(data).__name__}")
        try:
            return cls(**data)
        except TypeError as e:
            raise AcStateError(f"bad AC state {data!r}: {e}") from None

    def describe(self) -> str:
        """Short text, e.g. 'ON cool 22C fan 3 vane auto'."""
        text = f"{self.mode} {self.temp}C fan {self.fan} vane {self.vane}"
        if self.powerful:
            text += " powerful"
        return f"ON {text}" if self.power else f"OFF ({text})"


@dataclass(frozen=True, slots=True)
class SentState:
    state: AcState
    at: float
    actor: str


class AcStateStore:
    """Last state sent per AC, in `state/ac_state.json`: {device_id: {state, at, actor}}.

    This is a cache, so a damaged file never stops the bot: it starts empty with a
    warning, and a bad entry is skipped rather than failing the whole file.
    """

    def __init__(self, path: Path):
        self._store = JsonStore(path, dict, validate=_validate_top)
        self.load_warning: str | None = None

    def load(self) -> None:
        try:
            self._store.load()
            self.load_warning = self._store.load_warning
        except StoreError as e:
            self._store.use_default()
            self.load_warning = f"{e}; starting with no remembered AC state"
            logger.warning(self.load_warning)

    def get(self, device_id: str) -> SentState | None:
        entry = self._store.data.get(device_id)
        if entry is None:
            return None
        try:
            at = entry["at"]
            if isinstance(at, bool) or not isinstance(at, (int, float)):
                raise TypeError(f"'at' must be a number, got {at!r}")
            return SentState(AcState.from_dict(entry["state"]), float(at), str(entry["actor"]))
        except (AcStateError, KeyError, TypeError, ValueError) as e:
            logger.warning("Ignoring stored AC state for %s: %s", device_id, e)
            return None

    async def put(self, device_id: str, state: AcState, actor: str, at: float | None = None) -> None:
        at = time.time() if at is None else at
        if not math.isfinite(at):
            raise ValueError(f"bad timestamp {at!r}")
        record = {"state": state.to_dict(), "at": at, "actor": actor}

        def apply(data: dict) -> None:
            data[device_id] = record

        await self._store.update(apply)


def _validate_top(data: Any) -> None:
    if not isinstance(data, dict):
        raise TypeError("ac_state.json must be an object of {\"<device id>\": {...}}")
