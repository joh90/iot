"""Message text. Everything user-supplied goes through h() (HTML escaping, B11)."""

from __future__ import annotations

import html

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode


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
/stats [days] - speed, failed sends, LLM health (default 7 days)"""

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
