"""Approved users. Auth is by Telegram user id only (B5); the name is just a label."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from iotbot.result import Actor, Result
from iotbot.store import JsonlLog, JsonStore, StoreError

logger = logging.getLogger(__name__)

USER_ID_RE = re.compile(r"[1-9][0-9]{0,15}")
# Only people may change who is approved; never the scheduler or the LLM
USER_ADMIN_SURFACES = ("slash", "button")

MAX_NAME = 32


def validate_users(data: Any) -> None:
    if not isinstance(data, dict):
        raise TypeError("users.json must be an object of {\"<user id>\": \"<name>\"}")
    for k, v in data.items():
        if not (isinstance(k, str) and USER_ID_RE.fullmatch(k)):
            raise ValueError(f"users.json: key {k!r} is not a positive numeric user id")
        if not isinstance(v, str):
            raise ValueError(f"users.json: name for {k} must be a string")


def parse_user_id(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    text = str(raw).strip() if raw is not None else ""
    return int(text) if USER_ID_RE.fullmatch(text) else None


def clean_name(raw: Any) -> str:
    """Printable, single-spaced, at most MAX_NAME characters."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(raw or ""))
    return " ".join(text.split())[:MAX_NAME].strip()


class UserService:
    def __init__(self, store: JsonStore, audit: JsonlLog):
        self.store = store
        self.audit = audit

    def is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and str(user_id) in self.store.data

    def name_of(self, user_id: int) -> str:
        return self.store.data.get(str(user_id)) or str(user_id)

    def list(self) -> list[tuple[int, str]]:
        return sorted(((int(k), v) for k, v in self.store.data.items()), key=lambda u: (u[1].lower(), u[0]))

    def _gate(self, actor: Actor) -> Result | None:
        if actor.surface not in USER_ADMIN_SURFACES:
            return Result.fail("forbidden", "Users can only be changed by a person, not automatically.")
        return None

    async def _save(self, apply: Any) -> tuple[Any, Result | None]:
        try:
            return await self.store.update(apply), None
        except (OSError, StoreError, ValueError, TypeError) as e:
            logger.error("Could not save users: %s", e)
            return None, Result.fail("store_error", "Could not save users.json; nothing changed.")

    async def _audit(self, record: dict) -> None:
        # Shielded: the change is already on disk, so its audit line must not be lost to a cancel
        await asyncio.shield(self.audit.append_async(record))

    async def add(self, actor: Actor, raw_id: Any, name: Any) -> Result:
        if (denied := self._gate(actor)):
            return denied
        uid = parse_user_id(raw_id)
        if uid is None:
            return Result.fail("bad_user_id", "User id must be a positive number. They can send /ping to see it.")
        name = clean_name(name)
        if not name:
            return Result.fail("bad_name", "Give the user a name, e.g. /adduser 12345 Alex")

        def apply(users: dict) -> bool:
            if str(uid) in users:
                return False
            users[str(uid)] = name
            return True

        added, err = await self._save(apply)
        if err:
            return err
        if not added:
            return Result.fail("exists", f"{self.name_of(uid)} ({uid}) is already approved.")
        await self._audit({"event": "user_added", "user_id": uid, "name": name,
                           "by": actor.user_id, "surface": actor.surface})
        return Result.success(f"Added {name} ({uid}).", data=uid)

    async def delete(self, actor: Actor, raw_id: Any) -> Result:
        if (denied := self._gate(actor)):
            return denied
        uid = parse_user_id(raw_id)
        if uid is None:
            return Result.fail("bad_user_id", "Invalid user id.")
        # Rechecked here, at delete time, not just when the prompt was shown (B13)
        if uid == actor.user_id:
            return Result.fail("self_delete", "You cannot remove yourself.")

        def apply(users: dict) -> str | None:
            return users.pop(str(uid), None)

        removed, err = await self._save(apply)
        if err:
            return err
        if removed is None:
            # Double tap / already removed by someone else: not an error worth a crash
            return Result.fail("not_found", f"User {uid} is not approved (already removed?).")
        await self._audit({"event": "user_removed", "user_id": uid, "name": removed,
                           "by": actor.user_id, "surface": actor.surface})
        return Result.success(f"Removed {removed} ({uid}).", data=uid)
