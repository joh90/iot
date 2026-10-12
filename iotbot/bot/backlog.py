"""Updates queued while the bot was offline.

Telegram keeps updates for 24h, so after a power cut or a crash loop the bot would
otherwise run every tap and command sent while it was down: an "aircon On" someone
gave up on hours ago, or a toggle pressed twice. Such updates are answered and dropped:

- a message older than MAX_AGE_S (by its Telegram timestamp);
- a message that was queued when the bot started and whose timestamp is more than
  MAX_AGE_S off either way: after a power cut the Pi (no RTC) can boot with its clock
  hours behind, which would make an old message look new;
- a button tap that was already queued when the bot started (taps carry no timestamp,
  so the queue is read once at startup and every update up to its newest id is old).

Telegram refuses answers to taps older than about 15s, so a dropped tap is reported
with a chat message instead of a popup.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from telegram import Bot, Update
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from iotbot import telemetry

logger = logging.getLogger(__name__)

MAX_AGE_S = 120
OFFLINE_TAP = "The bot was offline when you tapped this, so nothing was sent. Tap again if you still want it."


class Backlog:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.cutoff: int | None = None       # newest update id queued at startup
        self._clock = clock

    async def load(self, bot: Bot) -> None:
        """Before polling starts. Reading without an offset confirms nothing, so polling
        still receives these updates; this only notes which ones are old."""
        try:
            updates = await bot.get_updates(timeout=0)
        except TelegramError as e:
            logger.warning("Could not read queued updates (%s); only message age is checked", e)
            return
        if updates:
            self.cutoff = max(u.update_id for u in updates)
            logger.info("%d update(s) were queued while the bot was offline", len(updates))
            if len(updates) >= 100:
                # getUpdates returns at most 100: taps queued after those are not caught
                logger.warning("100+ updates were queued; taps beyond the first 100 may still run")

    def age_s(self, update: Update) -> float | None:
        msg = update.message
        return None if msg is None or msg.date is None else self._clock() - msg.date.timestamp()

    def stale(self, update: Update) -> str | None:
        """'message' / 'button' if `update` must not run, else None."""
        queued = self.cutoff is not None and update.update_id <= self.cutoff
        if (age := self.age_s(update)) is not None and (age > MAX_AGE_S or (queued and age < -MAX_AGE_S)):
            return "message"
        if update.callback_query is not None and queued:
            return "button"
        return None


async def gate(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every handler (group -1). Approved users are told; others get nothing."""
    data = context.application.bot_data
    backlog: Backlog | None = data.get("backlog")
    if backlog is None or not isinstance(update, Update) or (why := backlog.stale(update)) is None:
        return
    user = update.effective_user
    telemetry.note(outcome="dropped_offline")
    logger.info("Dropped a %s queued while offline (update %s, user %s)", why, update.update_id,
                user.id if user else None)
    try:
        if user is not None and data["ctx"].users.is_allowed(user.id):
            await _tell(update, context, backlog, why)
    except Exception:  # noqa: BLE001 -- never let a failed notice run the stale update
        logger.exception("Could not tell user about a dropped %s", why)
    raise ApplicationHandlerStop


async def _tell(update: Update, context: ContextTypes.DEFAULT_TYPE, backlog: Backlog, why: str) -> None:
    if why == "button":
        try:
            await update.callback_query.answer(OFFLINE_TAP, show_alert=True)
            return
        except TelegramError:
            pass                              # too old to answer: say it in the chat
        chat = update.effective_chat or update.effective_user
        await context.bot.send_message(chat.id, OFFLINE_TAP)
        return
    age = backlog.age_s(update) or 0
    when = f"{round(age / 60)} min ago " if age >= 60 else ""
    await update.message.reply_text(f"Not run: you sent this {when}while the bot was offline. "
                                    "Send it again if you still want it.")
