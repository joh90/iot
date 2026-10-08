"""Approved users. Auth is by Telegram user id only (B5); the name is just a label."""

from __future__ import annotations

from typing import Any

from iotbot.result import Actor, Result
from iotbot.store import JsonlLog, JsonStore

MAX_NAME = 32


def validate_users(data: Any) -> None:
    if not isinstance(data, dict):
        raise TypeError("users.json must be an object of {\"<user id>\": \"<name>\"}")
    for k, v in data.items():
        if not (isinstance(k, str) and k.lstrip("-").isdigit()):
            raise ValueError(f"users.json: key {k!r} is not a numeric user id")
        if not isinstance(v, str):
            raise ValueError(f"users.json: name for {k} must be a string")


def parse_user_id(raw: Any) -> int | None:
    try:
        uid = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return uid if uid > 0 else None


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

    async def add(self, actor: Actor, raw_id: Any, name: str) -> Result:
        uid = parse_user_id(raw_id)
        if uid is None:
            return Result.fail("bad_user_id", "User id must be a positive number. They can send /ping to see it.")
        name = " ".join((name or "").split())[:MAX_NAME]
        if not name:
            return Result.fail("bad_name", "Give the user a name, e.g. /adduser 12345 Alex")

        def apply(users: dict) -> bool:
            if str(uid) in users:
                return False
            users[str(uid)] = name
            return True

        if not await self.store.update(apply):
            return Result.fail("exists", f"{self.name_of(uid)} ({uid}) is already approved.")
        await self.audit.append_async({"event": "user_added", "user_id": uid, "name": name,
                                       "by": actor.user_id, "surface": actor.surface})
        return Result.success(f"Added {name} ({uid}).", data=uid)

    async def delete(self, actor: Actor, raw_id: Any) -> Result:
        uid = parse_user_id(raw_id)
        if uid is None:
            return Result.fail("bad_user_id", "Invalid user id.")
        # Rechecked here, at delete time, not just when the prompt was shown (B13)
        if uid == actor.user_id:
            return Result.fail("self_delete", "You cannot remove yourself.")

        def apply(users: dict) -> str | None:
            return users.pop(str(uid), None)

        removed = await self.store.update(apply)
        if removed is None:
            # Double tap / already removed by someone else: not an error worth a crash
            return Result.fail("not_found", f"User {uid} is not approved (already removed?).")
        await self.audit.append_async({"event": "user_removed", "user_id": uid, "name": removed,
                                       "by": actor.user_id, "surface": actor.surface})
        return Result.success(f"Removed {removed} ({uid}).", data=uid)
