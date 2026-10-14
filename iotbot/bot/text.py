"""Message text. Everything user-supplied goes through h() (HTML escaping, B11)."""

from __future__ import annotations

import html
import logging

from telegram import CallbackQuery, InaccessibleMessage, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

logger = logging.getLogger(__name__)


def h(value: object) -> str:
    return html.escape(str(value), quote=False)


def b(value: object) -> str:
    return f"<b>{h(value)}</b>"


def popup(text: str) -> str:
    """Callback popups are plain text (no markup, B26) and max 200 chars."""
    return text if len(text) <= 200 else text[:197] + "..."


def human_duration(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    hh, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = [f"{d}d"] if d else []
    if d or hh:
        parts.append(f"{hh}h")
    parts.append(f"{m}m")
    return " ".join(parts)


START = """{name}

Commands:
/ping - your user id (works for everyone)
/status - server, Broadlink devices, users
/list - rooms, devices and their buttons
/keyboard [room or device] - button remote
/on &lt;device&gt;, /off &lt;device&gt; - power
/d &lt;device&gt; &lt;action&gt; - e.g. /d bedroom_ac power on high
/user - approved users (remove)
/adduser &lt;user id&gt; &lt;name&gt; - approve a user
/stats [days] - speed, failed sends, LLM health (default 7 days)
/memory - what the chat assistant remembers (forget items)
/outside [area] - weather and air from NEA (default: home)

Or just type, e.g. "a bit cooler in the bedroom" or "off the office aircon at 7am weekdays"."""

NOT_ALLOWED = "You are not approved to use this bot. Send /ping and give your user id to an approved user."


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


async def safe_edit(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
    """Edit the button message in place; tolerate double taps and old messages (B27, B28)."""
    msg = query.message
    if msg is None or isinstance(msg, InaccessibleMessage):
        # Too old or inaccessible: send a fresh message instead
        await dm(query, text, markup)
        return False
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return True
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return False
        raise


async def dm(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if not query.from_user:
        return
    try:
        await query.get_bot().send_message(query.from_user.id, text, parse_mode=ParseMode.HTML,
                                           reply_markup=markup, disable_web_page_preview=True)
    except TelegramError as e:  # e.g. Forbidden: the user never opened a private chat
        logger.info("Could not message user %s: %s", query.from_user.id, e)
