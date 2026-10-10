"""Telegram command and button handlers (PTB 22, async)."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from functools import wraps
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from telegram import Bot, CallbackQuery, InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from iotbot.bot import acpicker as ap
from iotbot.bot import keyboards as kb
from iotbot.bot import wizard as wz
from iotbot.bot.callbacks import decode, encode
from iotbot.bot.text import NOT_ALLOWED, START, b, h, human_duration, popup
from iotbot.context import AppContext
from iotbot.result import Actor, Result, Surface
from iotbot.schedule import notify as sn
from iotbot.services.users import parse_user_id

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


MAX_TEXT = 4000  # Telegram limit is 4096; keep headroom


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split on line boundaries. Every line we build has balanced tags, so no tag is cut."""
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single huge line: hard cut (plain text only in practice)
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur or not chunks:
        chunks.append(cur)
    return chunks


async def reply(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    msg = update.effective_message
    if msg is None:
        return
    parts = split_text(text)
    for i, part in enumerate(parts):
        await msg.reply_text(part, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                             reply_markup=markup if i == len(parts) - 1 else None)


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
        await _dm(query, text, markup)
        return False
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return True
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return False
        raise


async def _dm(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if not query.from_user:
        return
    try:
        await query.get_bot().send_message(query.from_user.id, text, parse_mode=ParseMode.HTML,
                                           reply_markup=markup, disable_web_page_preview=True)
    except TelegramError as e:  # e.g. Forbidden: the user never opened a private chat
        logger.info("Could not message user %s: %s", query.from_user.id, e)


async def answer_or_message(query: CallbackQuery, text: str, alert: bool) -> None:
    """Answer the button; if the answer window expired (slow send/retry), message instead."""
    try:
        await query.answer(popup(text), show_alert=alert)
    except BadRequest as e:
        logger.info("Callback answer failed (%s); sending as a message", e)
        await _dm(query, h(text))


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
    if ctx.ac and ctx.ac.presets:
        # What the bot last sent; the physical remote can change the AC without the bot knowing
        lines.append("")
        lines.append(b("Aircons (last state sent by the bot)"))
        for dev_id, preset in ctx.ac.presets.items():
            sent = ctx.ac.last(dev_id)
            if sent:
                when = datetime.fromtimestamp(sent.at, tz)
                lines.append(f"- {h(dev_id)}: {h(sent.state.describe())} ({when:%d %b %H:%M}, {h(sent.actor)})")
            else:
                lines.append(f"- {h(dev_id)}: nothing sent yet (On = {h(preset.describe())})")
    elif not ctx.settings.ac_encoder:
        lines.append("AC encoder: off (AC_ENCODER=off), aircons send captured codes")
    if ctx.schedules is not None:
        lines.append("")
        lines += schedule_status(ctx, now)
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


def schedule_status(ctx: AppContext, now: datetime) -> list[str]:
    from iotbot.schedule import text as stx
    from iotbot.schedule import timing as stm
    svc = ctx.schedules
    all_ = list(svc.store.all().values())
    paused = sum(1 for s in all_ if not s.enabled or (s.paused_until and s.paused_until > now))
    timers = sum(1 for s in all_ if s.is_timer)
    out = [b("Schedules") + f": {len(all_) - timers} weekly, {timers} timers, {paused} paused"
           + (f", {len(svc.broken())} BROKEN" if svc.broken() else "")]
    runs = sorted(((r[0], s) for s in all_ if (r := stm.next_runs(s, now, svc.tz))), key=lambda x: (x[0], x[1].id))
    if runs:
        at, s = runs[0]
        out.append(f"Next: {h(stx.describe(s, now, svc.tz))} at {h(stx.fmt_at(at, now, svc.tz))}")
    if ctx.scheduler and ctx.scheduler.clock_state != "ok":
        out.append(f"Scheduler: {h(ctx.scheduler.clock_state)}")
    return out


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
    elif ns == sn.NS:
        await _schedule_notice_button(update, context, query, action, args)
    elif ns in (wz.NS, ap.NS):
        await _wizard_button(update, context, query, ns, action, args)
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
        await safe_edit(query, f"Select {h(args[0])} action",
                        kb.device_keyboard(reg, args[0], schedule_buttons(context, args[0])))
    elif action == "f" and len(args) == 2:
        r = await ctx.devices.run(args[0], args[1], actor_of(update, ctx, "button"),
                                  request_id=request_id(update))
        text = "\n".join([r.message, *("Note: " + w for w in r.warnings)])
        await answer_or_message(query, text, alert=not r.ok or bool(r.warnings))
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
    elif action == "info" and args and (uid := parse_user_id(args[0])):
        await query.answer(popup(f"{ctx.users.name_of(uid)}, user id {uid}"))
    elif action == "ask" and args:
        uid = parse_user_id(args[0])
        if uid is None or not ctx.users.is_allowed(uid):
            await query.answer("Already removed.")
            await safe_edit(query, "Approved users", kb.users_keyboard(ctx.users.list(), me))
            return
        if uid == me:
            await query.answer("You cannot remove yourself.", show_alert=True)
            return
        await query.answer()
        text = f"Remove {b(ctx.users.name_of(uid))} ({uid})?"
        if ctx.schedules is not None and (owned := ctx.schedules.owned_by(uid)):
            from iotbot.schedule import text as stx
            names = ", ".join(stx.name(s) for s in owned[:10]) + (", ..." if len(owned) > 10 else "")
            text += f"\nTheir {len(owned)} schedule(s) ({h(names)}) will be handed to you."
        await safe_edit(query, text, kb.confirm_remove_keyboard(uid))
    elif action == "del" and args:
        # The service rechecks self-delete and existence at this moment (B13)
        actor = actor_of(update, ctx, "button")
        r = await ctx.users.delete(actor, args[0])
        if r.ok and ctx.schedules is not None:
            moved = await ctx.schedules.reassign(actor, ctx.users.is_allowed, actor.user_id)
            if moved.ok and moved.data.get("ids"):
                r.message += f" {len(moved.data['ids'])} schedule(s) are now yours."
            elif not moved.ok:
                r.message += f" Their schedules could not be handed over: {moved.message}"
        await answer_or_message(query, r.message, alert=not r.ok)
        await safe_edit(query, "Approved users", kb.users_keyboard(ctx.users.list(), me))
    else:
        await query.answer("This button is no longer active.")


async def _schedule_notice_button(update: Update, context: ContextTypes.DEFAULT_TYPE, query: CallbackQuery,
                                  action: str, args: list[str]) -> None:
    """Buttons on schedule notices: [Undo] [Retry] [Skip next] [Pause]."""
    ctx = app_ctx(context)
    if ctx.notifier is None or ctx.schedules is None or not args:
        await query.answer("This button is no longer active.")
        return
    actor = actor_of(update, ctx, "button")
    rev = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    if action == "retry":
        # Can outlast Telegram's ~15s answer window (rediscovery, timeouts): answer now, report by message
        try:
            await query.answer("Retrying...")
        except TelegramError:
            pass
        r = await ctx.notifier.retry(actor, args[0])
        await _dm(query, h(r.message))
        return
    if action == "undo":
        r = await ctx.notifier.undo(actor, args[0])
    elif action == "skip":
        r = await ctx.schedules.skip(actor, args[0], rev=rev)
    elif action == "pause":
        r = await ctx.schedules.pause(actor, [args[0]], rev=rev)
    else:
        await query.answer("This button is no longer active.")
        return
    if r.error == "stale":
        r.message = "Already changed (maybe a double tap). /schedule shows the current state."
    await answer_or_message(query, r.message, alert=not r.ok)
    await ctx.notifier.changed(actor, r)


async def send_notice(bot: Bot, n: sn.Notice) -> None:
    """Deliver a schedule notice. Silent ones arrive without a sound (PLAN: fire messages)."""
    markup = None
    if n.buttons:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                                       for row in n.buttons])
    for i, part in enumerate(parts := split_text(n.text)):
        await bot.send_message(n.user_id, part, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                               disable_notification=n.silent,
                               reply_markup=markup if i == len(parts) - 1 else None)


def wizard_of(context: ContextTypes.DEFAULT_TYPE) -> wz.Wizard | None:
    ctx = app_ctx(context)
    if ctx.schedules is None:
        return None
    data = context.application.bot_data
    if "wizard" not in data:
        data["wizard"] = wz.Wizard(ctx)
    return data["wizard"]


def schedule_buttons(context: ContextTypes.DEFAULT_TYPE, device: str) -> list[InlineKeyboardButton]:
    w = wizard_of(context)
    if w is None or device not in w.schedulable():
        return []
    out = [InlineKeyboardButton("Timer", callback_data=encode(wz.NS, "-", "tm", device))] if w.can_timer(device) else []
    return out + [InlineKeyboardButton("Schedule", callback_data=encode(wz.NS, "-", "newfor", device))]


async def _show(query: CallbackQuery, screen: wz.Screen | None, note: str) -> None:
    await answer_or_message(query, note, alert=bool(note) and screen is None) if note else await query.answer()
    if screen is not None:
        await safe_edit(query, screen.text, screen.markup)


async def _wizard_button(update: Update, context: ContextTypes.DEFAULT_TYPE, query: CallbackQuery,
                         ns: str, action: str, args: list[str]) -> None:
    w = wizard_of(context)
    if w is None:
        await query.answer("Schedules are not available.")
        return
    actor = actor_of(update, app_ctx(context), "button")
    if ns == ap.NS:
        screen, note = await w.picker_tap(actor, action, args[0] if args else "", args[1] if len(args) > 1 else "")
    elif action == "-":
        screen, note = await w.menu_tap(actor, args[0] if args else "", args[1:])
    else:
        d = w.get_draft(action, actor.user_id)
        msg = query.message
        if d is not None and msg is not None and not isinstance(msg, InaccessibleMessage):
            d.chat_id, d.message_id = getattr(msg, "chat_id", None), getattr(msg, "message_id", None)
        screen, note = await w.tap(actor, action, args[0] if args else "", args[1:])
    await _show(query, screen, note)


@restricted
async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    w = wizard_of(context)
    if w is None:
        await reply(update, "Schedules are not available.")
        return
    if context.args:
        await schedule_line(update, context, w)
        return
    screen = w.menu()
    await reply(update, screen.text, screen.markup)


async def schedule_line(update: Update, context: ContextTypes.DEFAULT_TYPE, w: wz.Wizard) -> None:
    """`/schedule add ...` (preview card with Save), `tonight`, `list`, `help`."""
    from iotbot.bot import oneliner
    sub, rest = context.args[0].lower(), context.args[1:]
    if sub == "tonight":
        screen = w.tonight()
    elif sub == "list":
        screen = w.list_screen()
    elif sub == "add" and rest:
        try:
            d = oneliner.parse_add(w, update.effective_user.id, " ".join(rest))
        except oneliner.LineError as e:
            await reply(update, h(str(e)))
            return
        screen = w.preview(d)
    else:
        await reply(update, h(oneliner.USAGE))
        return
    await reply(update, screen.text, screen.markup)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain text: only used as the answer to "Type a time". Never answers anyone else
    (not restricted-with-reply: that would answer every stranger and group message)."""
    w = wizard_of(context)
    msg = update.effective_message
    user = update.effective_user
    if w is None or msg is None or user is None or not getattr(msg, "text", None):
        return
    if not app_ctx(context).users.is_allowed(user.id):
        return
    actor = actor_of(update, app_ctx(context), "button")
    d, screen, note = await w.typed(actor, msg.text)
    if d is None and not note:
        return
    if note:
        await reply(update, h(note))
    if screen is not None:
        if d.chat_id is not None and d.message_id is not None:
            try:
                await context.bot.edit_message_text(screen.text, chat_id=d.chat_id, message_id=d.message_id,
                                                     parse_mode=ParseMode.HTML, reply_markup=screen.markup)
                return
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    return
                logger.info("Could not edit the wizard message (%s); sending a new one", e)
            except TelegramError as e:
                logger.info("Could not edit the wizard message (%s); sending a new one", e)
        await reply(update, screen.text, screen.markup)


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
