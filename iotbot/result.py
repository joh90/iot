"""Result type shared by every service call (slash, buttons, scheduler, later the LLM)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Result:
    ok: bool
    message: str = ""                 # short human-readable outcome
    data: Any = None
    error: str = ""                   # machine-friendly error code, e.g. "not_found"
    warnings: list[str] = field(default_factory=list)
    # Never resolved silently: callers (buttons now, LLM later) decide what to do
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def success(cls, message: str = "", data: Any = None, **kw: Any) -> Result:
        return cls(True, message, data, **kw)

    @classmethod
    def fail(cls, error: str, message: str, **kw: Any) -> Result:
        return cls(False, message, error=error, **kw)


@dataclass(frozen=True, slots=True)
class Actor:
    """Who asked, and through which surface: slash | button | scheduler | llm | system."""
    user_id: int
    name: str
    surface: str
