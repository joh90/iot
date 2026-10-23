"""/phone (Phase 8): set up your phone for presence, your offer settings, and the
taps on welcome-home / everyone-out offers."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Bot, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from iotbot import telemetry
from iotbot.bot.callbacks import encode
from iotbot.bot.text import b, dm, h, popup, reply, safe_edit
from iotbot.context import AppContext
from iotbot.presence import offers as po
from iotbot.presence.tracker import PAIR_WATCH_S, Presence
from iotbot.result import Actor

logger = logging.getLogger(__name__)

NS = "ph"
OFF = "Presence is switched off on this bot (PRESENCE=off, or the aircon encoder is off)."
INTRO = ("The bot can offer to turn aircons on when you get home and off when everyone has left. "
         "It checks whether your phone is on the home Wi-Fi; only home or away is kept, on the Pi, "
         "plus your arrive/leave times for weekly patterns (you can delete them).")
# Per person: devices found by the last setup, and the running watch (in memory)
_found: dict[int, list[tuple[str, str]]] = {}
_watch: dict[int, asyncio.Task] = {}


def _btn(label: str, *parts: object) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=encode(NS, *parts))


def phone_screen(presence: Presence, uid: int, tz: str, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    ph = presence.phone(uid)
    lines = [note] if note else []
    if ph is None:
        lines += [b("Your phone: not set up"), h(INTRO)]
        rows = [[_btn("Set up my phone", "setup"), _btn("Close", "close")]]
        return "\n".join(lines), InlineKeyboardMarkup(rows)
    p = presence.person(uid)
    seen = (f"seen {datetime.fromtimestamp(p.last_seen, ZoneInfo(tz)):%H:%M}" if p.last_seen else "not seen yet")
    state = {"home": "home now", "away": "away", "unknown": "checking"}[p.state]
    arr, dep = ph.get("arrival", True), ph.get("departure", True)
    home, paired = presence.counts()
    lines += [b("Your phone: set up") + f", {state} ({seen})",
              f"Arrival offers: {'on' if arr else 'off'}    Departure offers: {'on' if dep else 'off'}",
              f"Home now: {home} of {paired} phones"]
    rows = [[_btn(f"Arrival {'off' if arr else 'on'}", "arr", int(not arr)),
             _btn(f"Departure {'off' if dep else 'on'}", "dep", int(not dep))],
            [_btn("Delete my history", "forget"), _btn("Remove phone", "remove")],
            [_btn("Close", "close")]]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def cmd_phone(update: Update, context: ContextTypes.DEFAULT_TYPE, ctx: AppContext) -> None:
    if ctx.presence is None:
        await reply(update, OFF)
        return
    await reply(update, *phone_screen(ctx.presence, update.effective_user.id, ctx.settings.timezone))


async def phone_button(context: ContextTypes.DEFAULT_TYPE, query: CallbackQuery, ctx: AppContext, actor: Actor,
                       action: str, args: list[str]) -> None:
    presence, uid, tz = ctx.presence, actor.user_id, ctx.settings.timezone
    if presence is None:
        await query.answer(OFF, show_alert=True)
        return
    if action == "close":
        await query.answer()
        await safe_edit(query, "Closed. /phone to open again.")
    elif action in ("setup", "again"):
        if presence.phone(uid) is not None and action == "setup":
            await query.answer()
            await safe_edit(query, *phone_screen(presence, uid, tz))
            return
        if uid in _watch and not _watch[uid].done():
            await query.answer("Already watching; follow the steps above.")
            return
        await query.answer()
        await safe_edit(query, "Looking at the home Wi-Fi (about 15 seconds)...")
        msg = query.message
        _watch[uid] = asyncio.create_task(_setup(context.bot, presence, uid, msg.chat_id, msg.message_id),
                                          name=f"phone-setup-{uid}")
    elif action == "cancel":
        task = _watch.pop(uid, None)
        if task:
            task.cancel()
        _found.pop(uid, None)
        await query.answer("Cancelled.")
        await safe_edit(query, *phone_screen(presence, uid, tz, "Setup cancelled."))
    elif action == "yes" and args and args[0].isdecimal() and int(args[0]) < len(_found.get(uid, [])):
        mac, ip = _found.pop(uid)[int(args[0])]
        await presence.pair(uid, mac, ip)
        telemetry.note(outcome="ok")
        await query.answer("Phone set up.")
        await safe_edit(query, *phone_screen(presence, uid, tz, "Done: your phone is set up."))
    elif action in ("arr", "dep") and args and args[0] in ("0", "1") and presence.phone(uid):
        await presence.set_pref(uid, "arrival" if action == "arr" else "departure", args[0] == "1")
        await query.answer("Saved.")
        await safe_edit(query, *phone_screen(presence, uid, tz))
    elif action == "forget":
        await query.answer()
        await safe_edit(query, "Delete all your arrive/leave times? Your phone stays set up.",
                        InlineKeyboardMarkup([[_btn("Yes, delete", "forgety"), _btn("No", "back")]]))
    elif action == "forgety":
        n = await presence.forget_history(uid)
        await query.answer("Deleted.")
        await safe_edit(query, *phone_screen(presence, uid, tz, f"Deleted {n} arrive/leave entries."))
    elif action == "remove":
        await query.answer()
        await safe_edit(query, "Remove your phone? Offers stop and you no longer count as home or away.",
                        InlineKeyboardMarkup([[_btn("Yes, remove", "removey"), _btn("No", "back")]]))
    elif action == "removey":
        await presence.unpair(uid)
        await query.answer("Removed.")
        await safe_edit(query, *phone_screen(presence, uid, tz, "Your phone was removed."))
    elif action == "back":
        await query.answer()
        await safe_edit(query, *phone_screen(presence, uid, tz))
    else:
        await query.answer("This button is no longer active. /phone to start again.")


async def _setup(bot: Bot, presence: Presence, uid: int, chat_id: int, message_id: int) -> None:
    """Runs in the background (up to ~2.5 min), editing the /phone message as it goes."""
    _found.pop(uid, None)              # a Yes on an older setup message must not pair anything
    async def show(text: str, rows: list[list[InlineKeyboardButton]] | None = None) -> None:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.HTML,
                                        reply_markup=InlineKeyboardMarkup(rows) if rows else None)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    async def ready(count: int) -> None:
        await show(f"Watching the home Wi-Fi for {PAIR_WATCH_S // 60} minutes ({count} devices on it).\n"
                   "Turn your phone's Wi-Fi off, wait 10 seconds, then turn it back on.",
                   [[_btn("Cancel", "cancel")]])

    try:
        try:
            found = await presence.watch_for_toggle(ready)
        except RuntimeError:
            await show("Someone else is setting up a phone right now. Try again in 2 minutes.",
                       [[_btn("Try again", "again"), _btn("Cancel", "cancel")]])
            return
        if len(found) == 1:
            _found[uid] = found
            await show("1 device dropped off the Wi-Fi and came back. Is this your phone?",
                       [[_btn("Yes, that's mine", "yes", 0), _btn("No, try again", "again")]])
        else:
            _found.pop(uid, None)
            what = "no device" if not found else f"{len(found)} devices"
            await show(f"Saw {what} drop off and come back, so I can't tell which is yours.\n"
                       "Try again, and check your phone's Private Wi-Fi Address is not set to Rotating.",
                       [[_btn("Try again", "again"), _btn("Cancel", "cancel")]])
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 -- a background task: report, never crash the bot
        logger.exception("Phone setup failed")
        try:
            await show("Setup failed (see the bot log). /phone to try again.")
        except TelegramError:
            pass
    finally:
        _watch.pop(uid, None)


async def offer_button(query: CallbackQuery, ctx: AppContext, actor: Actor, action: str, args: list[str]) -> None:
    if ctx.offers is None:
        await query.answer("This offer is no longer active.")
        return
    sends = action in ("on", "off", "undo")
    if sends:                            # IR can outlast Telegram's answer window: answer first
        try:
            await query.answer("Working on it...")
        except TelegramError:
            pass
    r = await ctx.offers.tap(actor, action, args)      # redraws every copy, this one included
    telemetry.note_result(r.ok, r.error)
    try:
        if not sends:
            await query.answer(popup(r.message), show_alert=not r.ok)
        elif not r.ok:
            await dm(query, h(r.message))
    except TelegramError:
        pass


async def stop_setups() -> None:
    """At shutdown: end any phone setup still watching."""
    tasks = list(_watch.values())
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def offer_io(bot: Bot) -> tuple[po.Send, po.Edit]:
    """The send/edit coroutines the offers layer uses."""
    def markup(rows: po.Rows) -> InlineKeyboardMarkup | None:
        return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row]
                                     for row in rows]) if rows else None

    async def send(uid: int, text: str, rows: po.Rows, silent: bool) -> int | None:
        m = await bot.send_message(uid, text, parse_mode=ParseMode.HTML, reply_markup=markup(rows),
                                   disable_notification=silent)
        return m.message_id

    async def edit(uid: int, mid: int, text: str, rows: po.Rows) -> None:
        try:
            await bot.edit_message_text(text, chat_id=uid, message_id=mid, parse_mode=ParseMode.HTML,
                                        reply_markup=markup(rows))
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    return send, edit
