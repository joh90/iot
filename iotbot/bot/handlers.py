"""Telegram command and button handlers (PTB 22, async)."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from functools import wraps
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from telegram import CallbackQuery, InaccessibleMessage, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from iotbot.bot import keyboards as kb
from iotbot.bot.callbacks import decode
from iotbot.bot.text import NOT_ALLOWED, START, b, h, human_duration, popup
from iotbot.context import AppContext
from iotbot.result import Actor, Result, Surface

logger = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]


def app_ctx(context: ContextTypes.DEFAULT_TYPE) -> AppContext:
    return context.application.bot_data["ctx"]


def actor_of(update: Update, ctx: AppContext, surface: Surface) -> Actor:
    user = update.effective_user
    return Actor(user.id, ctx.users.name_of(user.id), surface)


def request_id(update: Update) -> str:
    return f"tg:{update.update_id}"


def restricted(func: Handler) -> Handler:
    """Allow approved user ids only (B5: id, never username)."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        ctx = app_ctx(context)
        user = update.effective_user
        if user is not None and ctx.users.is_allowed(user.id):
            return await func(update, context)
        logger.info("Rejected user %s", user.id if user else None)
        if update.callback_query:
            await update.callback_query.answer(NOT_ALLOWED, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(NOT_ALLOWED)
        return None

    return wrapper


async def reply(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                             disable_web_page_preview=True)


async def reply_result(update: Update, r: Result) -> None:
    text = h(r.message)
    if r.warnings:
        text += "\n" + "\n".join("Note: " + h(w) for w in r.warnings)
    await reply(update, text)


async def safe_edit(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
    """Edit the button message in place; tolerate double taps and old messages (B27, B28)."""
    msg = query.message
    if msg is None or isinstance(msg, InaccessibleMessage):
        # Too old or inaccessible: send a fresh message instead
        if query.from_user:
            await query.get_bot().send_message(query.from_user.id, text, parse_mode=ParseMode.HTML,
                                               reply_markup=markup)
        return False
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return True
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return False
        raise


# ---- commands ------------------------------------------------------------------------

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open to everyone, so a new user can find their id."""
    user = update.effective_user
    who = user.full_name if user else "there"
    await reply(update, f"PONG\nHi {b(who)}, your user id is <code>{user.id if user else '?'}</code>")


@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, START.format(name=b(app_ctx(context).settings.bot_name)))


@restricted
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = app_ctx(context)
    tz = ZoneInfo(ctx.settings.timezone)
    now = datetime.now(tz)
    lines = [
        f"Server time: {now:%Y-%m-%d %H:%M:%S}",
        f"Uptime: {human_duration(time.time() - ctx.started_at)}",
    ]
    if ctx.devices.last:
        dev, last = max(ctx.devices.last.items(), key=lambda kv: kv[1].at)
        when = datetime.fromtimestamp(last.at, tz)
        status = "ok" if last.ok else "in progress" if last.ok is None else "FAILED"
        lines.append(f"Last action: {h(dev)} {h(last.feature)} by {h(last.actor)} at {when:%H:%M:%S} ({status})")
    lines.append("")
    lines.append(b("Broadlink devices"))
    for st in ctx.hub.status.values():
        state = "online" if st.online else "OFFLINE"
        detail = f"{h(st.type)} {h(st.ip)}".strip()
        lines.append(f"- {h(st.label)}: {state}" + (f", {detail}" if detail else "")
                     + (f" ({h(st.error)})" if st.error else ""))
        lines += [f"  note: {h(w)}" for w in st.warnings]
    if not ctx.hub.status:
        lines.append("- none configured")
    lines.append("")
    lines.append(f"Rooms: {len(ctx.registry.rooms)}, devices: {len(ctx.registry.devices)}")
    lines.append("Approved users: " + h(", ".join(name for _, name in ctx.users.list())))
    if ctx.warnings:
        lines.append("")
        lines.append(b(f"Config warnings ({len(ctx.warnings)})"))
        lines += [f"- {h(w)}" for w in ctx.warnings[:10]]
        if len(ctx.warnings) > 10:
            lines.append("- ... see the log for the rest")
    await reply(update, "\n".join(lines))


@restricted
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = app_ctx(context)
    reg = ctx.registry
    if not reg.devices:
        await reply(update, f"No devices yet. Add rooms and devices to {h(ctx.settings.devices_path.name)}.")
        return
    blocks = []
    for room in reg.rooms.values():
        if not room.devices:
            continue
        lines = [b(room.name)]
        if room.rm_mac:
            st = ctx.hub.status.get(room.rm_mac)
            online = "online" if st and st.online else "offline"
            lines.append(f"RM: {h(room.rm_type or '?')} {h(room.rm_mac)} ({online})")
        for dev_id in room.devices:
            d = reg.devices[dev_id]
            feats = ", ".join(f.label for f in d.features.values()) or "no captured codes"
            lines.append(f"- <code>{h(d.id)}</code> ({h(d.type_name)}): {h(feats)}")
        blocks.append("\n".join(lines))
    await reply(update, "\n\n".join(blocks))


@restricted
async def cmd_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reg = app_ctx(context).registry
    target = (context.args or [""])[0]
    if target in reg.rooms and reg.rooms[target].devices:
        await reply(update, f"Select {h(target)} device", kb.room_keyboard(reg, target))
    elif target in reg.devices:
        await reply(update, f"Select {h(target)} action", kb.device_keyboard(reg, target))
    elif not any(r.devices for r in reg.rooms.values()):
        await reply(update, "No devices yet.")
    else:
        if target:
            await reply(update, f"Room or device {b(target)} not found.")
        await reply(update, kb.ROOMS_TEXT, kb.rooms_keyboard(reg))


async def _run_device(update: Update, context: ContextTypes.DEFAULT_TYPE, device_id: str, words: list[str]) -> None:
    ctx = app_ctx(context)
    r = ctx.devices.resolve(device_id, words)
    if not r.ok:
        await reply_result(update, r)
        return
    r = await ctx.devices.run(r.data["device"], r.data["feature"], actor_of(update, ctx, "slash"),
                              request_id=request_id(update))
    await reply_result(update, r)


def _power(word: str) -> Handler:
    @restricted
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await reply(update, f"Usage: /{word} &lt;device&gt;. /list shows device ids.")
            return
        await _run_device(update, context, context.args[0], [word])
    handler.__name__ = f"cmd_{word}"
    return handler


cmd_on = _power("on")
cmd_off = _power("off")


@restricted
async def cmd_device(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await reply(update, "Usage: /d &lt;device&gt; &lt;action&gt;, e.g. /d bedroom_ac power on")
        return
    await _run_device(update, context, args[0], args[1:])


@restricted
async def cmd_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = app_ctx(context)
    await reply(update, "Approved users", kb.users_keyboard(ctx.users.list(), update.effective_user.id))


@restricted
async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) < 2:
        await reply(update, "Usage: /adduser &lt;user id&gt; &lt;name&gt;\n"
                            "The new user can send /ping to see their id.")
        return
    ctx = app_ctx(context)
    r = await ctx.users.add(actor_of(update, ctx, "slash"), args[0], " ".join(args[1:]))
    await reply_result(update, r)


# ---- buttons -------------------------------------------------------------------------

@restricted
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = decode(query.data)
    ns, action, args = parts[0], (parts[1] if len(parts) > 1 else ""), parts[2:]
    if ns == kb.KB:
        await _remote_button(update, context, query, action, args)
    elif ns == kb.US:
        await _users_button(update, context, query, action, args)
    else:
        # Buttons from the old bot or a removed feature
        await query.answer("This button is no longer active.")


async def _remote_button(update: Update, context: ContextTypes.DEFAULT_TYPE, query: CallbackQuery,
                         action: str, args: list[str]) -> None:
    ctx = app_ctx(context)
    reg = ctx.registry
    if action == "close":
        await query.answer()
        await safe_edit(query, "Closed. /keyboard to open again.")
    elif action == "rooms":
        await query.answer()
        await safe_edit(query, kb.ROOMS_TEXT, kb.rooms_keyboard(reg))
    elif action == "r" and args and args[0] in reg.rooms:
        await query.answer()
        await safe_edit(query, f"Select {h(args[0])} device", kb.room_keyboard(reg, args[0]))
    elif action == "d" and args and args[0] in reg.devices:
        await query.answer()
        await safe_edit(query, f"Select {h(args[0])} action", kb.device_keyboard(reg, args[0]))
    elif action == "f" and len(args) == 2:
        r = await ctx.devices.run(args[0], args[1], actor_of(update, ctx, "button"),
                                  request_id=request_id(update))
        await query.answer(popup(r.message), show_alert=not r.ok)
    else:
        await query.answer("That room or device no longer exists. /keyboard to refresh.", show_alert=True)


async def _users_button(update: Update, context: ContextTypes.DEFAULT_TYPE, query: CallbackQuery,
                        action: str, args: list[str]) -> None:
    ctx = app_ctx(context)
    me = update.effective_user.id
    if action == "close":
        await query.answer()
        await safe_edit(query, "Closed. /user to open again.")
    elif action == "list":
        await query.answer()
        await safe_edit(query, "Approved users", kb.users_keyboard(ctx.users.list(), me))
    elif action == "add":
        await query.answer("Use /adduser <user id> <name>", show_alert=True)
    elif action == "me":
        await query.answer("This is you.")
    elif action == "info" and args and args[0].isdigit():
        await query.answer(popup(f"{ctx.users.name_of(int(args[0]))}, user id {args[0]}"))
    elif action == "ask" and args:
        uid = args[0]
        if not uid.isdigit() or not ctx.users.is_allowed(int(uid)):
            await query.answer("Already removed.")
            await safe_edit(query, "Approved users", kb.users_keyboard(ctx.users.list(), me))
            return
        if int(uid) == me:
            await query.answer("You cannot remove yourself.", show_alert=True)
            return
        await query.answer()
        await safe_edit(query, f"Remove {b(ctx.users.name_of(int(uid)))} ({h(uid)})?",
                        kb.confirm_remove_keyboard(int(uid)))
    elif action == "del" and args:
        # The service rechecks self-delete and existence at this moment (B13)
        r = await ctx.users.delete(actor_of(update, ctx, "button"), args[0])
        await query.answer(popup(r.message), show_alert=not r.ok)
        await safe_edit(query, "Approved users", kb.users_keyboard(ctx.users.list(), me))
    else:
        await query.answer("This button is no longer active.")


# ---- errors --------------------------------------------------------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the update id and the exception only -- no message text or user data (B15)."""
    uid = getattr(update, "update_id", None)
    logger.error("Error handling update %s", uid, exc_info=context.error)
    if uid is None:
        return  # not caused by an update (e.g. a job)
    try:
        if getattr(update, "callback_query", None):
            # Stop the button spinner (B15)
            await update.callback_query.answer("Something went wrong; check the bot log.", show_alert=True)
        elif getattr(update, "effective_message", None):
            await update.effective_message.reply_text("Something went wrong; check the bot log.")
    except Exception:  # noqa: BLE001 -- the error handler itself must never raise
        logger.debug("Could not report error to user", exc_info=True)
