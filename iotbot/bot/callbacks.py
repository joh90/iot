"""Compact string callback data that survives restarts (B18, adversarial #2).

Format `<ns>:<action>[:<arg>...]`, max 64 bytes (Telegram limit). Ids never contain
':' (see model.ID_RE), so splitting is unambiguous.
"""

from __future__ import annotations

MAX_BYTES = 64


class CallbackTooLong(ValueError):
    pass


def encode(*parts: object) -> str:
    data = ":".join(str(p) for p in parts)
    if len(data.encode("utf-8")) > MAX_BYTES:
        raise CallbackTooLong(data)
    return data


def decode(data: str | None) -> list[str]:
    return (data or "").split(":")
