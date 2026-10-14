"""One assistant turn: model -> read tools -> model ... until an answer or a write (Phase 4.1).

Bounds: at most MAX_TOOL_ROUNDS rounds of tool calls (a write on the call after them is still
accepted: it needs no further round) and TURN_TIMEOUT_S for the whole turn.
A WRITE call ends the loop: code, not the model, writes the reply (a confirm card), because
in the 4.0 spike the model claimed "Both aircons are now off" while the card was pending.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from iotbot.llm.client import LlmClient, LlmError, Reply
from iotbot.llm.tools import ArgError, Tool, parse_args

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 3
TURN_TIMEOUT_S = 20.0
MAX_WRITES = 10                 # bulk cap: the card lists every item
MAX_RESULT_CHARS = 4000


@dataclass(slots=True)
class Action:
    tool: Tool
    args: dict[str, Any]
    prepared: Any = None                        # the card line (llm.cards.Prepared), if the tool prepares


@dataclass(slots=True)
class TurnResult:
    kind: str                                   # answer | actions | failed
    text: str = ""                              # answer text (kind answer)
    actions: list[Action] = field(default_factory=list)
    error: str | None = None                    # LlmError code, or rounds|empty|too_many|truncated|bug
    rounds: int = 0                             # model calls made
    llm_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str | None = None
    tool_calls: list[str] = field(default_factory=list)   # "name" or "name!" when refused
    exchanges: list[dict[str, Any]] = field(default_factory=list)

    def note(self, reply: Reply) -> None:
        self.rounds += 1
        self.llm_ms += reply.ms
        self.prompt_tokens += reply.prompt_tokens or 0
        self.completion_tokens += reply.completion_tokens or 0
        self.model = reply.model or self.model
        self.exchanges.append(reply.exchange)


async def run_turn(llm: LlmClient, messages: list[dict[str, Any]], tools: list[Tool],
                   timeout_s: float = TURN_TIMEOUT_S, check_slot: bool = True) -> TurnResult:
    """`messages` = system + context + the new user message. Tool rounds go into a copy, so
    the caller's list never holds half a round (tool calls without results). Never raises."""
    out = TurnResult("failed")
    try:
        async with asyncio.timeout(timeout_s):
            if check_slot:
                await llm.check_slot()
            await _loop(llm, list(messages), tools, out)
    except TimeoutError:
        out.kind, out.error = "failed", "timeout"
    except LlmError as e:
        out.kind, out.error = "failed", e.code
        logger.info("LLM turn failed: %s", e)
    except Exception:  # noqa: BLE001 -- a chat turn must end in a reply, never a crash
        logger.exception("LLM turn crashed")
        out.kind, out.error = "failed", "bug"
    if out.kind != "actions":
        out.actions = []
    return out


@dataclass(slots=True)
class Checked:
    id: str
    tool: Tool | None
    args: dict[str, Any] | None
    error: str | None


def _check_calls(reply: Reply, by_name: dict[str, Tool]) -> list[Checked]:
    out = []
    for c in reply.tool_calls:
        tool = by_name.get(c.name)
        if tool is None:
            out.append(Checked(c.id, None, None, f"unknown tool {c.name}"))
            continue
        try:
            out.append(Checked(c.id, tool, parse_args(tool, c.arguments), None))
        except ArgError as e:
            out.append(Checked(c.id, tool, None, str(e)))
    return out


async def _loop(llm: LlmClient, messages: list[dict[str, Any]], tools: list[Tool], out: TurnResult) -> None:
    by_name = {t.name: t for t in tools}
    specs = [t.spec() for t in tools]
    for _ in range(MAX_TOOL_ROUNDS + 1):
        reply = await llm.chat(messages, specs)
        out.note(reply)
        if reply.truncated:
            # A cut answer would read as complete; a cut tool call is broken JSON
            out.kind, out.error = "failed", "truncated"
            return
        if not reply.tool_calls:
            if not reply.content:
                out.kind, out.error = "failed", "empty"
                return
            out.kind, out.text = "answer", reply.content
            return

        checked = _check_calls(reply, by_name)
        out.tool_calls += [(c.tool.name if c.tool else "?") + ("!" if c.error else "") for c in checked]
        bad = [c for c in checked if c.error]
        writes = [c for c in checked if c.tool and c.tool.write and not c.error]
        if writes and not bad:
            # Valid reads in the same round are dropped: the card is the reply
            actions: list[Action] = []
            for c in writes:
                a = Action(c.tool, c.args)                     # type: ignore[arg-type]
                if a not in actions:                           # the same call twice is one card line
                    actions.append(a)
            if len(actions) > MAX_WRITES:
                out.kind, out.error = "failed", "too_many"
                return
            refused = await _prepare(actions)
            if not refused:
                out.kind, out.actions = "actions", actions
                return
            # Back to the model, e.g. "bed_ac is off"; the other writes are "not done" this round
            for c in checked:
                if c.tool and (c.tool.name, _key(c.args)) in refused:
                    c.error = refused[(c.tool.name, _key(c.args))]
            out.tool_calls = out.tool_calls[:-len(checked)] + [
                (c.tool.name if c.tool else "?") + ("!" if c.error else "") for c in checked]
        if out.rounds > MAX_TOOL_ROUNDS:
            break                                # it wants yet another tool round

        # Bad calls go back as errors (a partial card would be worse than a retry);
        # with no bad calls, run the reads. Writes mixed with errors are not run.
        messages.append(reply.assistant_message())
        for c in checked:
            if c.error is not None:
                result = _dump({"error": c.error})
            elif c.tool.write:                                 # type: ignore[union-attr]
                result = _dump({"error": "not done: fix the other calls in this round first"})
            else:
                result = await _run_read(c.tool, c.args)        # type: ignore[arg-type]
            messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
    out.kind, out.error = "failed", "rounds"


async def _prepare(actions: list[Action]) -> dict[tuple[str, str], str]:
    """Build every card line. Returns {(tool, args): reason} for the calls that cannot be done."""
    refused = {}
    for a in actions:
        if a.tool.prepare is None:
            continue
        try:
            a.prepared = await a.tool.prepare(a.args)
        except ArgError as e:
            refused[(a.tool.name, _key(a.args))] = str(e)
        except Exception as e:  # noqa: BLE001 -- a prepare bug is a refused call, not a crashed turn
            logger.exception("Preparing %s failed", a.tool.name)
            refused[(a.tool.name, _key(a.args))] = f"could not prepare this ({e.__class__.__name__})"
    return refused


def _key(args: dict[str, Any] | None) -> str:
    return json.dumps(args, sort_keys=True)


async def _run_read(tool: Tool, args: dict[str, Any]) -> str:
    if tool.run is None:
        return _dump({"error": "this tool is not available"})
    try:
        return _dump(await tool.run(args))
    except Exception as e:  # noqa: BLE001 -- a broken read tool must not end the turn
        logger.exception("Read tool %s failed", tool.name)
        return _dump({"error": f"tool failed ({e.__class__.__name__})"})


def _dump(result: Any) -> str:
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "...(cut)"
