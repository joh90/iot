"""Free text -> the assistant -> one reply (Phase 4.2).

Shows "typing..." while the model works (Telegram clears it after ~5 s, so it is
re-sent), then sends one message: the answer, a confirm card, or a short failure line.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from iotbot import telemetry
from iotbot.bot.text import h, reply
from iotbot.llm.assistant import MAX_INPUT_CHARS, Assistant, Outcome
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


async def show_actions(update: Update, context: ContextTypes.DEFAULT_TYPE, assistant: Assistant,
                       actor: Actor, text: str, o: Outcome) -> None:
    """Placeholder until 4.3 draws confirm cards: lists the actions, runs nothing."""
    lines = [f"- {a.tool.name} {a.args}" for a in o.turn.actions] if o.turn else []
    said = "Proposed (not done):\n" + "\n".join(lines)
    assistant.remember(actor.user_id, text, said)
    await reply(update, h(said))
