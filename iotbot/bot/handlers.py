"""Telegram command and button handlers (PTB 22, async)."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from telegram import Bot, CallbackQuery, InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from iotbot import telemetry
from iotbot.bot import acpicker as ap
from iotbot.bot import keyboards as kb
from iotbot.bot import stats
from iotbot.bot import wizard as wz
from iotbot.bot.callbacks import decode, encode
from iotbot.bot import chat
from iotbot.bot.chat import on_chat
from iotbot.bot.text import NOT_ALLOWED, START, b, dm, h, human_duration, popup, reply, safe_edit, split_text
from iotbot.context import AppContext
from iotbot.outside.weather import REGIONS
from iotbot.store import read_since
from iotbot.result import Actor, Result, Surface
from iotbot.schedule import notify as sn
from iotbot.schedule import text as tx
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


async def private_only(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs first (group -2): the bot works in private chats only. In a group nothing answers,
    so /status, /list or /memory never show there (security review 2026-10-15, #13)."""
    chat = getattr(update, "effective_chat", None)
    if chat is not None and chat.type != ChatType.PRIVATE:
        telemetry.note(outcome="not_private")
        logger.info("Ignored an update from a %s chat", chat.type)
        raise ApplicationHandlerStop


def restricted(func: Handler) -> Handler:
    """Allow approved user ids only (B5: id, never username)."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        ctx = app_ctx(context)
        user = update.effective_user
        if user is not None and ctx.users.is_allowed(user.id):
            return await func(update, context)
        logger.info("Rejected user %s", user.id if user else None)
        telemetry.note(outcome="denied")
        if update.callback_query:
            await update.callback_query.answer(NOT_ALLOWED, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(NOT_ALLOWED)
        return None

    return wrapper


async def reply_result(update: Update, r: Result) -> None:
    telemetry.note_result(r.ok, r.error)     # refusals that never reached a device call
    text = h(r.message)
    if r.warnings:
        text += "\n" + "\n".join("Note: " + h(w) for w in r.warnings)
    await reply(update, text)


async def answer_or_message(query: CallbackQuery, text: str, alert: bool) -> None:
    """Answer the button; if the answer window expired (slow send/retry), message instead."""
    try:
        await query.answer(popup(text), show_alert=alert)
    except BadRequest as e:
        logger.info("Callback answer failed (%s); sending as a message", e)
        await dm(query, h(text))


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


_STATS_LOCK = asyncio.Lock()


@restricted
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = app_ctx(context)
    args = context.args or []
    days = stats.DEFAULT_DAYS
    if args:
        if len(args) > 1 or not args[0].isascii() or not args[0].isdigit() \
                or not 1 <= int(args[0]) <= stats.MAX_DAYS:
            await reply(update, f"Usage: /stats [days], 1 to {stats.MAX_DAYS} (default {stats.DEFAULT_DAYS}).")
            return
        days = int(args[0])
    now = datetime.now(ZoneInfo(ctx.settings.timezone))
    since = now - timedelta(days=days)

    def load() -> list[str]:
        # Up to a year of lines: parsed off the event loop
        turns = read_since(ctx.tracker.log, since, now, stats.TURN_KEYS) if ctx.tracker else []
        events = read_since(ctx.devices.events, since, now, stats.EVENT_KEYS)
        return stats.summary(turns, events, ctx.registry, days, ctx.users.name_of)

    async with _STATS_LOCK:          # one at a time: a year of logs is heavy on a Pi 3
        lines, llm = await asyncio.gather(asyncio.to_thread(load), stats.llm_health(ctx.settings.llm_url))
    await reply(update, "\n".join([*lines, "", b("Assistant PC (LLM)"), f"- {llm} ({h(ctx.settings.llm_url)})"]))


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
async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = app_ctx(context)
    await chat.cmd_memory(update, context, ctx.memory, actor_of(update, ctx, "slash"))


@restricted
async def cmd_outside(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Weather and air from NEA without the LLM (works when the PC is off)."""
    ctx = app_ctx(context)
    text = " ".join(context.args or [])
    area = region = None
    if text.lower() in REGIONS:
        region = text.lower()                                  # "/outside west": that region's air
    elif text:
        hits = ctx.outside.match_area(text)
        if len(hits) != 1:
            why = (f"'{h(text[:40])}' could be {h(', '.join(hits[:6]))}" if hits
                   else f"I don't know the area '{h(text[:40])}'")
            await reply(update, f"{why}. Use an NEA area such as Bedok, Jurong West or Toa Payoh, a region "
                                "(north, south, east, west, central), or just /outside for home.")
            return
        area = hits[0]
    telemetry.note(intent="/outside")
    await reply(update, h(await ctx.outside.summary(area, region)))


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
        await reply(update, device_text(app_ctx(context), target), kb.device_keyboard(reg, target, device_rows(context, target)))
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
    elif ns == ap.NS and action.startswith(AC_CARD):
        await _ac_card_button(update, context, query, action[len(AC_CARD):], args)
    elif ns == chat.NS:
        await _card_button(update, context, query, action, args)
    elif ns == chat.MEM:
        ctx = app_ctx(context)
        await chat.memory_button(query, ctx.memory, actor_of(update, ctx, "button"), action, args)
    elif ns in (wz.NS, ap.NS):
        await _wizard_button(update, context, query, ns, action, args)
    else:
        # Buttons from the old bot or a removed feature
        await query.answer("This button is no longer active.")


async def _card_button(update: Update, context: ContextTypes.DEFAULT_TYPE, query: CallbackQuery,
                       action: str, args: list[str]) -> None:
    ctx = app_ctx(context)
    await chat.card_button(update, context, query, ctx.assistant, actor_of(update, ctx, "llm"), action, args)


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
        await _show_device(context, query, args[0])
    elif action == "q" and len(args) == 2:
        await _quick_ac(update, context, query, args[0], args[1])
    elif action == "set" and args:
        await _open_ac_card(context, query, args[0])
    elif action == "f" and len(args) == 2:
        r = await ctx.devices.run(args[0], args[1], actor_of(update, ctx, "button"),
                                  request_id=request_id(update))
        text = "\n".join([r.message, *("Note: " + w for w in r.warnings)])
        await answer_or_message(query, text, alert=not r.ok or bool(r.warnings))
        if r.ok and _managed_ac(ctx, args[0]):
            await _show_device(context, query, args[0], refresh=True)     # the screen shows the AC's new state
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
        if not ctx.users.is_admin(me):
            await query.answer("Only an admin can approve or remove users.", show_alert=True)
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
    """Buttons on schedule notices: [Undo] [Retry] [Skip next|Don't skip] [Pause|Resume]."""
    ctx = app_ctx(context)
    if ctx.notifier is None or ctx.schedules is None or not args:
        await query.answer("This button is no longer active.")
        return
    actor = actor_of(update, ctx, "button")
    rev = int(args[1]) if len(args) > 1 and args[1].isdecimal() else None
    if action == "retry":
        # Can outlast Telegram's ~15s answer window (rediscovery, timeouts): answer now, report by message
        try:
            await query.answer("Retrying...")
        except TelegramError:
            pass
        r = await ctx.notifier.retry(actor, args[0])
        telemetry.note_result(r.ok, r.error)
        await dm(query, h(r.message))
        return
    if action == "undo":
        r = await ctx.notifier.undo(actor, args[0])
    elif action == "skip":
        r = await ctx.schedules.skip(actor, args[0], rev=rev)
    elif action == "pause":
        r = await ctx.schedules.pause(actor, [args[0]], rev=rev)
    elif action == "resume":
        r = await ctx.schedules.resume(actor, [args[0]], rev=rev)
    elif action == "unskip" and len(args) > 2 and (day := tx.parse_day(args[2])):
        r = await ctx.schedules.unskip(actor, args[0], day, rev=rev)
    else:
        await query.answer("This button is no longer active.")
        return
    telemetry.note_result(r.ok, r.error)
    if r.error == "stale":
        r.message = "Already changed (maybe a double tap); the buttons now show the current state."
    await answer_or_message(query, r.message, alert=not r.ok)
    await ctx.notifier.changed(actor, r)
    if action != "undo":               # args[0] is a schedule id, not an Undo token
        await _redraw_notice(query, ctx.notifier, args[0])


NOW_LINE = "\n\nNow: "


async def _redraw_notice(query: CallbackQuery, notifier: sn.Notifier, sid: str) -> None:
    """Rebuild a notice's buttons (and its 'Now:' line) from the schedule's state, so
    Pause becomes Resume. Best effort: an old or deleted message is left alone."""
    msg = query.message
    if msg is None or isinstance(msg, InaccessibleMessage) or not getattr(msg, "text", None):
        return
    markup = getattr(msg, "reply_markup", None)
    buttons = [(btn.text, btn.callback_data) for row in (markup.inline_keyboard if markup else ())
               for btn in row if isinstance(btn.callback_data, str)]
    line, rows = notifier.controls(sid, buttons, failed=msg.text.startswith(sn.FAILED))
    text = msg.text_html.split(NOW_LINE)[0] + (NOW_LINE + h(line) if line else "")
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                                   for row in rows]) if rows else None
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                      disable_web_page_preview=True)
    except TelegramError as e:          # best effort: the tap itself already worked
        if "not modified" not in str(e).lower():
            logger.info("Could not redraw schedule notice: %s", e)


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


# ---- AC quick buttons and the Settings card ---------------------------------------------

AC_CARD = "!"   # picker owner prefix for the Settings card; wizard draft ids never start with it


def _managed_ac(ctx: AppContext, device: str) -> bool:
    return ctx.devices.ac is not None and ctx.devices.ac.manages(device)


def device_text(ctx: AppContext, device: str) -> str:
    text = f"Select {h(device)} action"
    if _managed_ac(ctx, device):
        last = ctx.devices.ac.last(device)
        text += f"\nLast sent: {h(last.state.describe())}" if last else "\nNothing sent yet"
    return text


async def _show_device(context: ContextTypes.DEFAULT_TYPE, query: CallbackQuery, device: str,
                       refresh: bool = False) -> None:
    """`refresh`: an update after a send, already reported by popup. Best effort: never posts
    a new message for an old keyboard, and a failed edit is not an error."""
    ctx = app_ctx(context)
    text, markup = device_text(ctx, device), kb.device_keyboard(ctx.registry, device, device_rows(context, device))
    if not refresh:
        await safe_edit(query, text, markup)
        return
    if query.message is None or isinstance(query.message, InaccessibleMessage):
        return
    try:
        await safe_edit(query, text, markup)
    except TelegramError as e:
        logger.info("Could not refresh the %s screen: %s", device, e)


def device_rows(context: ContextTypes.DEFAULT_TYPE, device: str) -> list[list[InlineKeyboardButton]]:
    """Rows under a device's own buttons: AC quick buttons, then [Settings] [Timer] [Schedule]."""
    rows: list[list[InlineKeyboardButton]] = []
    last = schedule_buttons(context, device)
    if _managed_ac(app_ctx(context), device):
        rows.append([InlineKeyboardButton(label, callback_data=encode(kb.KB, "q", device, op))
                     for op, label in ap.QUICK.items()])
        last = [InlineKeyboardButton("Settings", callback_data=encode(kb.KB, "set", device)), *last]
    return rows + [last]


async def _quick_ac(update: Update, context: ContextTypes.DEFAULT_TYPE, query: CallbackQuery,
                    device: str, op: str) -> None:
    """-1C / +1C / Swing / Fan: the last state the bot sent, one thing changed, sent in full.
    Never turns an AC on: an AC that is off (or never sent to) gets a popup and nothing is sent."""
    ctx = app_ctx(context)
    changes = ap.quick_changes(op, ctx.devices.ac.presets[device]) if _managed_ac(ctx, device) else None
    if changes is None:
        await query.answer("This button is no longer active.")
        return
    r = await ctx.devices.adjust_ac_state(device, changes, actor_of(update, ctx, "button"),
                                          request_id=request_id(update), skip_same=True)
    if r.error == "ac_off":
        r.message = f"{device} is off as far as the bot knows. Press On first."
    elif r.error == "ac_state_unknown":
        r.message = f"The bot has not sent anything to {device} yet. Press On first."
    text = "\n".join([r.message, *("Note: " + w for w in r.warnings)])
    await answer_or_message(query, text, alert=r.error not in ("", "no_change") or bool(r.warnings))
    if r.ok:
        await _show_device(context, query, device, refresh=True)


def _ac_card(ctx: AppContext, device: str, st) -> tuple[str, InlineKeyboardMarkup]:
    text = ap.picker_text(device, st, "Send now")
    last = ctx.devices.ac.last(device)
    if last is None or not last.state.power:
        text += "\nThe AC is off as far as the bot knows; Send now turns it on."
    return h(text), ap.picker_keyboard(AC_CARD + device, st, "Send now")


async def _open_ac_card(context: ContextTypes.DEFAULT_TYPE, query: CallbackQuery, device: str) -> None:
    """Starts from the last state sent if the AC is on, else from the room preset."""
    ctx = app_ctx(context)
    if not _managed_ac(ctx, device):
        await query.answer("This button is no longer active.")
        return
    last = ctx.devices.ac.last(device)
    st = last.state if last and last.state.power else ctx.devices.ac.presets[device]
    await query.answer()
    await safe_edit(query, *_ac_card(ctx, device, st.with_changes(power=True)))


async def _ac_card_button(update: Update, context: ContextTypes.DEFAULT_TYPE, query: CallbackQuery,
                          device: str, args: list[str]) -> None:
    """The whole card state rides in the button, so a tap needs no memory and survives restarts."""
    ctx = app_ctx(context)
    code, op = (args + ["", ""])[:2]
    st = ap.decode_state(code)
    if st is None or not _managed_ac(ctx, device):
        await query.answer("This button is no longer active.")
        return
    if op == "x":
        await query.answer()
        await _show_device(context, query, device)
    elif op == "noop":
        await query.answer(f"{st.temp}C")
    elif op == "done":
        # Explicit, so this one may turn the AC on
        r = await ctx.devices.send_ac_state(device, st, actor_of(update, ctx, "button"),
                                            request_id=request_id(update))
        text = "\n".join([r.message, *("Note: " + w for w in r.warnings)])
        await answer_or_message(query, text, alert=not r.ok or bool(r.warnings))
        if r.ok:
            # Back to the device screen: no [Send now] left behind to replay by mistake later
            await _show_device(context, query, device, refresh=True)
    else:
        new = ap.apply_op(st, op, ctx.devices.ac.presets[device])
        await query.answer()
        if new != st:
            await safe_edit(query, *_ac_card(ctx, device, new))


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
    """Plain text from approved users. If the wizard waits for a time and this is one, the
    wizard takes it; otherwise the wizard stops waiting and, in a private chat, the text goes
    to the assistant. Strangers and groups get no reply (that would answer every message)."""
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or not getattr(msg, "text", None):
        return
    ctx = app_ctx(context)
    if not ctx.users.is_allowed(user.id):
        return
    chat = update.effective_chat
    w = wizard_of(context)
    if w is not None and w.waiting_for(user.id, chat.id if chat else None):
        if wz.parse_time(msg.text) is not None:
            await _wizard_text(update, context, w, msg.text)
            return
        w.awaiting.pop(user.id, None)
    if chat is None or chat.type != ChatType.PRIVATE:
        return
    await on_chat(update, context, ctx.assistant, actor_of(update, ctx, "llm"), msg.text)


async def _wizard_text(update: Update, context: ContextTypes.DEFAULT_TYPE, w: wz.Wizard, text: str) -> None:
    actor = actor_of(update, app_ctx(context), "button")
    d, screen, note = await w.typed(actor, text)
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
    telemetry.note(outcome="error", error=context.error.__class__.__name__ if context.error else None)
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
