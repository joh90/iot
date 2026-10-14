"""Free text -> the assistant -> one reply (Phase 4.2).

Shows "typing..." while the model works (Telegram clears it after ~5 s, so it is
re-sent), then sends one message: the answer, a confirm card, or a short failure line.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from iotbot import telemetry
from iotbot.bot.callbacks import encode
from iotbot.bot.text import b, h, popup, reply, safe_edit
from iotbot.llm.assistant import MAX_INPUT_CHARS, Assistant, Outcome
from iotbot.llm.cards import CARD_TTL_S, Card, CardError, Done, Prepared
from iotbot.result import Actor

logger = logging.getLogger(__name__)

TYPING_EVERY_S = 4.0
FALLBACK = "use /keyboard or /schedule"

OFF = f"The chat assistant is switched off; {FALLBACK}."
FAILED = {
    "busy": f"The assistant is busy right now. Try again in a minute, or {FALLBACK}.",
    "down": f"The assistant is not reachable right now (the PC may be off). Please {FALLBACK}.",
    "timeout": f"That took too long, so I stopped. Try again, or {FALLBACK}.",
    "too_many": "That is more than 10 changes at once. Please split it up.",
}
FAILED_OTHER = f"Sorry, I could not work that out. Try saying it another way, or {FALLBACK}."


def outcome_text(o: Outcome) -> str:
    """The reply for everything except a card (plain text, HTML-escaped)."""
    if o.kind == "answer":
        return h(o.text)
    if o.kind == "busy_you":
        return "Still working on your last message."
    if o.kind == "too_long":
        return f"That message is too long for me (over {MAX_INPUT_CHARS} characters)."
    if o.kind == "limited":
        return f"That is a lot of messages. Try again in {max(1, round(o.wait_s / 60))} min, or {FALLBACK}."
    error = o.turn.error if o.turn else None
    return FAILED.get(error or "", FAILED_OTHER)


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    while True:
        try:
            await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception as e:  # noqa: BLE001 -- "typing..." is cosmetic
            logger.debug("No typing action: %s", e)
        await asyncio.sleep(TYPING_EVERY_S)


async def on_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, assistant: Assistant | None,
                  actor: Actor, text: str) -> None:
    if assistant is None:
        telemetry.note(outcome="llm_off")
        await reply(update, OFF)
        return
    chat_id = update.effective_chat.id
    typing = asyncio.create_task(_keep_typing(context, chat_id))
    try:
        o = await assistant.handle(actor, text)
    finally:
        typing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing
    telemetry.note(intent=f"llm:{o.kind}", outcome=_outcome(o))
    if o.turn and o.turn.rounds:
        telemetry.note(llm_ms=o.turn.llm_ms, tokens=o.turn.prompt_tokens + o.turn.completion_tokens)
    if o.kind == "actions":
        await show_actions(update, context, assistant, actor, text, o)
        return
    await reply(update, outcome_text(o))


def _outcome(o: Outcome) -> str:
    if o.kind in ("answer", "actions"):
        return "ok"
    return o.turn.error if o.kind == "failed" and o.turn and o.turn.error else o.kind


# ---- confirm cards (4.3) -----------------------------------------------------------------

NS = "lc"


def _btn(label: str, op: str, card: Card) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=encode(NS, op, card.id))


def _lines(card: Card, marks: list[str] | None = None) -> list[str]:
    return [f"- {b(p.name)}: {h(p.line)}" + (f"  {h(marks[i])}" if marks and marks[i] else "")
            for i, p in enumerate(card.items)]


def card_screen(card: Card) -> tuple[str, InlineKeyboardMarkup]:
    text = "Do this?\n" + "\n".join(_lines(card)) + f"\nExpires in {CARD_TTL_S // 60} min."
    return text, InlineKeyboardMarkup([[_btn("Do it", "do", card), _btn("Cancel", "x", card)]])


def _mark(r: Done | None) -> str:
    if r is None:
        return ""
    return "ok" if r.ok else f"FAILED ({r.text})"


def result_screen(card: Card, who: str, at: str) -> tuple[str, InlineKeyboardMarkup | None]:
    ok = sum(1 for r in card.results if r and r.ok)
    head = f"Done (by {h(who)}, {at}):" if ok == len(card.items) else f"Done ({ok} of {len(card.items)}):"
    text = head + "\n" + "\n".join(_lines(card, [_mark(r) for r in card.results]))
    row = []
    if any(r and not r.ok for r in card.results):
        row.append(_btn("Retry failed", "retry", card))
    if any(r and r.ok and r.undo for r in card.results):
        row.append(_btn("Undo", "undo", card))
    return text, InlineKeyboardMarkup([row]) if row else None


def closed_screen(card: Card, why: str) -> str:
    return h(why) + "\n" + "\n".join(_lines(card))


def _context_note(card: Card, note: str, marks: list[str] | None = None) -> None:
    if card.context is not None:
        card.context["content"] = note + "\n" + "\n".join(
            f"- {p.name}: {p.line}" + (f" ({marks[i]})" if marks and marks[i] else "")
            for i, p in enumerate(card.items))


async def show_actions(update: Update, context: ContextTypes.DEFAULT_TYPE, assistant: Assistant,
                       actor: Actor, text: str, o: Outcome) -> None:
    items = [a.prepared for a in o.turn.actions] if o.turn else []
    if not items or any(not isinstance(p, Prepared) for p in items):
        # A write tool without a card line is a coding mistake: never run it blind
        logger.error("Chat action without a card line: %s", [a.tool.name for a in o.turn.actions])
        telemetry.note(outcome="bug", error="no card line")
        await reply(update, h(FAILED_OTHER))
        return
    card, replaced = assistant.cards.new(actor.user_id, items)
    body, markup = card_screen(card)
    sent = await update.effective_message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)
    card.chat_id, card.message_id = getattr(sent, "chat_id", None), getattr(sent, "message_id", None)
    card.context = assistant.remember(actor.user_id, text, "")
    _context_note(card, "Card shown, waiting for the person to tap Do it:")
    for old in replaced:
        _context_note(old, "Card replaced by a newer request; nothing was done:")
        if old.chat_id is not None and old.message_id is not None:
            with contextlib.suppress(TelegramError):
                await context.bot.edit_message_text(closed_screen(old, "Replaced by your newer request; nothing "
                                                                       "was done."), chat_id=old.chat_id,
                                                    message_id=old.message_id, parse_mode=ParseMode.HTML)


async def card_button(update: Update, context: ContextTypes.DEFAULT_TYPE, query: CallbackQuery,
                      assistant: Assistant | None, actor: Actor, op: str, args: list[str]) -> None:
    if assistant is None or not args:
        await query.answer("This button is no longer active.")
        return
    cards = assistant.cards
    try:
        card = cards.get(args[0], actor.user_id)
        if op == "x":
            cards.cancel(card)
        else:
            cards.begin(card, op)               # every refusal happens here, before the one answer
    except CardError as e:
        telemetry.note(outcome="refused")
        await query.answer(popup(str(e)), show_alert=True)
        card = cards.cards.get(args[0])
        if card is not None and card.user_id == actor.user_id and card.status in ("expired", "replaced"):
            await safe_edit(query, closed_screen(card, str(e)))
        return
    if op == "x":
        _context_note(card, "Card cancelled by the person; nothing was done:")
        telemetry.note(outcome="cancelled")
        await query.answer("Cancelled.")
        await safe_edit(query, closed_screen(card, "Cancelled; nothing was done."))
        return
    await query.answer("Working on it...")
    results = await cards.run(card, op, actor)
    now = assistant.now().strftime("%H:%M")
    _note_results(results)
    if op == "undo":
        marks = ["not done, nothing to undo" if r is None else _mark(r) for r in results]
        _context_note(card, "The person tapped Undo:", marks)
        text = f"Undone (by {h(actor.name)}, {now}):\n" + "\n".join(_lines(card, marks))
        retry = InlineKeyboardMarkup([[_btn("Undo again", "undo", card)]]) if card.status == "done" else None
        await safe_edit(query, text, retry)
        return
    _context_note(card, "The person tapped Do it:", [_mark(r) for r in card.results])
    await safe_edit(query, *result_screen(card, actor.name, now))


def _note_results(results: list[Done | None]) -> None:
    failed = [r.text for r in results if r is not None and not r.ok]
    telemetry.note(outcome="ok" if not failed else "failed", error=failed[0] if failed else None)
