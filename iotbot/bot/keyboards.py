"""Inline keyboards for the remote (/keyboard) and users (/user)."""

from __future__ import annotations

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup

from iotbot.bot.callbacks import encode
from iotbot.devices.model import Registry

KB = "kb"   # remote namespace
US = "us"   # users namespace

ROOMS_TEXT = "Select room"


def _rows(buttons: list[Btn], cols: int) -> list[list[Btn]]:
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]


def close_button(ns: str) -> Btn:
    return Btn("Close", callback_data=encode(ns, "close"))


def rooms_keyboard(reg: Registry) -> InlineKeyboardMarkup:
    rooms = [Btn(r, callback_data=encode(KB, "r", r)) for r, room in reg.rooms.items() if room.devices]
    return InlineKeyboardMarkup(_rows(rooms, 2) + [[close_button(KB)]])


def room_keyboard(reg: Registry, room: str) -> InlineKeyboardMarkup:
    devs = [Btn(d, callback_data=encode(KB, "d", d)) for d in reg.rooms[room].devices]
    return InlineKeyboardMarkup(_rows(devs, 2) + [[
        Btn("<- Rooms", callback_data=encode(KB, "rooms")), close_button(KB)]])


def device_keyboard(reg: Registry, device_id: str) -> InlineKeyboardMarkup:
    """Only features with captured codes get a button (B17)."""
    dev = reg.devices[device_id]
    feats = [Btn(f.label, callback_data=encode(KB, "f", dev.id, f.key)) for f in dev.features.values()]
    return InlineKeyboardMarkup(_rows(feats, 2) + [[
        Btn("<- Back", callback_data=encode(KB, "r", dev.room)),
        Btn("Rooms", callback_data=encode(KB, "rooms")),
        close_button(KB),
    ]])


def users_keyboard(users: list[tuple[int, str]], viewer_id: int) -> InlineKeyboardMarkup:
    rows = []
    for uid, name in users:
        label = f"{name} ({uid})"
        if uid == viewer_id:
            rows.append([Btn(label + " - you", callback_data=encode(US, "me"))])
        else:
            rows.append([Btn(label, callback_data=encode(US, "info", uid)),
                         Btn("Remove", callback_data=encode(US, "ask", uid))])
    rows.append([Btn("Add user", callback_data=encode(US, "add")), close_button(US)])
    return InlineKeyboardMarkup(rows)


def confirm_remove_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        Btn("Yes, remove", callback_data=encode(US, "del", uid)),
        Btn("No", callback_data=encode(US, "list")),
    ]])
