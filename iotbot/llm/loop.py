"""One assistant turn: model -> read tools -> model ... until an answer or a write (Phase 4.1).

Bounds: at most MAX_TOOL_ROUNDS rounds of tool calls (a write on the call after them is still
accepted: it needs no further round) and TURN_TIMEOUT_S for the whole turn.
A WRITE call ends the loop: code, not the model, writes the reply (a confirm card), because
in the 4.0 spike the model claimed "Both aircons are now off" while the card was pending.
Text the model writes next to its write calls is kept as a short note above the card (the
answer to "... and also check X"), unless it claims anything was done.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from iotbot.llm.client import LlmClient, LlmError, Reply
from iotbot.llm.tools import AlreadyError, ArgError, Tool, parse_args

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 3
TURN_TIMEOUT_S = 20.0
MAX_WRITES = 10                 # bulk cap: the card lists every item
MAX_RESULT_CHARS = 4000
MAX_NOTE_CHARS = 600
# An answer that claims an action or a card: only a tool call does anything
CLAIM_RE = re.compile(
    r"\bcards? (is |are |has been |was )?(shown|sent|ready|below|above|drawn)\b|\[card\b|"
    r"\btap (do it|remember|save)\b|\bdo this\?|"
    r"\b(i will|i'll|i am going to|i'm going to|let me) (now )?(turn|set|switch|change|make|run|start|stop)\b|"
    r"\bturning (it |the \w+ )?(on|off) (now|for you)\b|\b(has|have) been (turned|set|switched|changed)\b|"
    r"\b(i've|i have|done|ok)[,.!]? .{0,40}\bis now (on|off|set)\b|"
    r"\b(i've|i have|i will|i'll) (now )?(noted|note|saved|save|remembered|remember|stored|store)\b|"
    r"\bnoted that\b", re.IGNORECASE)
# A sentence that is only a result: "Bedroom set to 23." / "Okay, both rooms turned off." A diary
# line goes on ("... at 23:00 by the schedule") and a state answer says "is set to": neither matches
_WORD = r"(?!(is|was|are|were|be|been|being|still|already|stays|remains)\b)[\w-]+"   # not "is set to"
SHORT_CLAIM_RE = re.compile(
    rf"((ok|okay|done|sure)[,.!]?\s+)?(the |both )?({_WORD}\s+){{0,2}}?{_WORD}:?\s+(now\s+)?"
    r"(set to \d+\s*(°\s*c|c)?( fan \w+)?|turned (on|off)|switched (on|off))\s*[.!]?", re.IGNORECASE)
# A sentence above a card must not read as a result ("Done!", "I have added ...") or be about
# the card itself ("ready for you to confirm"): such sentences are dropped from the note
NOTE_DROP_RE = re.compile(
    r"\b(done|saved|remembered|noted|stored|confirm\w*|card|all set)\b|"
    r"\b(is|are) now\b|\b(turned|switched) (\w+ )?(on|off)\b|"
    r"\b(turning|switching|adding|pausing|resuming|deleting|removing|scheduling|setting up)\b|"
    r"\b(will|going to|goes|go) (be )?(turn|switch|go|switched|turned)\w* (on|off)\b|\bgoes (on|off)\b|"
    r"\b(i|i've|i have|i'll|i will|i've just|i just|we) (have |will |just |now )?(added|created|set up|set|"
    r"scheduled|paused|resumed|deleted|removed|changed|made|turned|switched|updated)\b|"
    r"\b(is|are|has been|have been|will be) (set to |being |now )?(added|created|set up|scheduled|"
    r"deleted|removed|changed|updated|turn(ed)? (on|off)|switch(ed)? (on|off))\b|"
    r"^\W*(ok|okay|sure|got it|here you go|here it is|-)?\W*$", re.IGNORECASE)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
# Tool-call markup in plain text: the server's parser missed a (malformed) call
TOOL_MARKUP_RE = re.compile(r"<tool_call>|</?function[=>]|<parameter=|</tool_call>")


@dataclass(slots=True)
class Action:
    tool: Tool
    args: dict[str, Any]
    prepared: Any = None                        # the card line (llm.cards.Prepared), if the tool prepares


@dataclass(slots=True)
class TurnResult:
    kind: str                                   # answer | actions | failed
    text: str = ""                              # answer text, or the note above a card (kind actions)
    actions: list[Action] = field(default_factory=list)
    error: str | None = None                    # LlmError code, or rounds|empty|too_many|truncated|bug
    note_error: str | None = None               # the card is fine, but the call for its note failed
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
                   timeout_s: float = TURN_TIMEOUT_S, check_slot: bool = True, ask_rest: bool = False) -> TurnResult:
    """`messages` = system + context + the new user message. Tool rounds go into a copy, so
    the caller's list never holds half a round (tool calls without results). `ask_rest`: the
    message looks like it also asks something, so a card made without any read gets one more
    model call for that answer. Never raises."""
    out = TurnResult("failed")
    try:
        async with asyncio.timeout(timeout_s):
            if check_slot:
                await llm.check_slot()
            await _loop(llm, list(messages), tools, out, ask_rest)
    except TimeoutError:
        _failed(out, "timeout")
    except LlmError as e:
        _failed(out, e.code)
        logger.info("LLM turn failed: %s", e)
    except Exception:  # noqa: BLE001 -- a chat turn must end in a reply, never a crash
        logger.exception("LLM turn crashed")
        _failed(out, "bug")
    if out.kind != "actions":
        out.actions = []
    return out


def _failed(out: TurnResult, code: str) -> None:
    """A ready card survives a failed answer call: it shows without its note."""
    if out.kind == "actions" and out.actions:
        out.note_error = code
    else:
        out.kind, out.error = "failed", code


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


async def _loop(llm: LlmClient, messages: list[dict[str, Any]], tools: list[Tool], out: TurnResult,
                ask_rest: bool = False) -> None:
    by_name = {t.name: t for t in tools}
    specs = [t.spec() for t in tools]
    nudged = False
    already: list[str] = []                      # refused because nothing would change: the card says so
    pending: list[Action] | None = None          # a ready card, waiting for the model's answer to the rest
    had_reads = False                            # a check was looked up in an earlier round

    def card(actions: list[Action], text: str | None) -> None:
        out.kind, out.actions, out.error = "actions", actions, None
        out.text = _note(text, _subjects(actions)) or ("Left out: " + "; ".join(already) + "." if already else "")

    for _ in range(MAX_TOOL_ROUNDS + 1):
        reply = await llm.chat(messages, specs)
        out.note(reply)
        if reply.truncated:
            # A cut answer would read as complete; a cut tool call is broken JSON
            if pending:
                card(pending, None)
            else:
                out.kind, out.error = "failed", "truncated"
            return
        if not reply.tool_calls:
            if pending:
                card(pending, reply.content)             # its words become the note; a claim is dropped
                return
            if not reply.content:
                out.kind, out.error = "failed", "empty"
                return
            if claims(reply.content):
                # Text that announces an action or a card, without the tool call: no card exists
                out.tool_calls.append("claim!")
                if out.rounds > MAX_TOOL_ROUNDS or nudged:
                    out.kind, out.error = "failed", "fake_card"
                    return
                nudged = True
                messages += [{"role": "assistant", "content": reply.content},
                             {"role": "user", "content": "(System: nothing was done or saved and no card "
                                                         "exists. To change or remember anything, call the "
                                                         "tool now; otherwise just answer.)"}]
                continue
            if not TOOL_MARKUP_RE.search(reply.content):
                out.kind, out.text = "answer", reply.content
                return
            # A tool call the server could not parse came back as text: never show it, retry
            out.tool_calls.append("?!")
            if out.rounds > MAX_TOOL_ROUNDS:
                out.kind, out.error = "failed", "bad_tool_call"
                return
            messages += [{"role": "assistant", "content": "(unreadable tool call)"},
                         {"role": "user", "content": "(System: your last tool call could not be read. "
                                                     "Call the tool again with valid JSON arguments.)"}]
            continue

        checked = _check_calls(reply, by_name)
        out.tool_calls += [(c.tool.name if c.tool else "?") + ("!" if c.error else "") for c in checked]
        bad = [c for c in checked if c.error]
        writes = [c for c in checked if c.tool and c.tool.write and not c.error]
        reads = [c for c in checked if c.tool and not c.tool.write and not c.error]
        ready = False
        if writes and pending:
            # The card is ready and told "do not call it again": never drop or swap its lines
            for c in writes:
                c.error = "the card is already made; do not call write tools again, answer in text"
            writes = []
        if writes and not bad:
            actions: list[Action] = []
            for c in writes:
                a = Action(c.tool, c.args)                     # type: ignore[arg-type]
                if a not in actions:                           # the same call twice is one card line
                    actions.append(a)
            if len(actions) > MAX_WRITES:
                out.kind, out.error = "failed", "too_many"
                return
            refused = await _prepare(actions, already)
            # A card with nothing else asked (or no rounds left) is the reply now
            asked = reads or ((had_reads or ask_rest) and not _note(reply.content, _subjects(actions)))
            if not refused and (not asked or out.rounds > MAX_TOOL_ROUNDS):
                card(actions, reply.content)
                return
            if not refused:
                # "Do X, and check Y": the card is ready; run the reads and let the model answer Y.
                # Committed now, so a failed or slow answer call still shows the card (no note).
                pending, ready = actions, True
                card(actions, None)
            else:
                # Back to the model, e.g. "bed_ac is off"; the other writes are "not done" this round
                for c in checked:
                    if c.tool and (c.tool.name, _key(c.args)) in refused:
                        c.error = refused[(c.tool.name, _key(c.args))]
                out.tool_calls = out.tool_calls[:-len(checked)] + [
                    (c.tool.name if c.tool else "?") + ("!" if c.error else "") for c in checked]
        if out.rounds > MAX_TOOL_ROUNDS:
            if pending:
                card(pending, None)                    # out of rounds: the card without an answer
                return
            break                                      # it wants yet another tool round

        # Bad calls go back as errors (a partial card would be worse than a retry);
        # with no bad calls, run the reads. Writes mixed with errors are not run.
        had_reads = had_reads or bool(reads)
        messages.append(reply.assistant_message())
        for c in checked:
            if c.error is not None:
                result = _dump({"error": c.error})
            elif c.tool.write and ready:                       # type: ignore[union-attr]
                result = _dump({"card": "ready: shown to the person after your reply, for them to confirm. "
                                        "Do not call it again. If their message also asks or checks something, "
                                        "answer that in one or two short sentences (use read tools if needed), "
                                        "without saying this is done; if not, reply with just: -"})
            elif c.tool.write:                                 # type: ignore[union-attr]
                result = _dump({"error": "not done: fix the other calls in this round first"})
            else:
                result = await _run_read(c.tool, c.args)        # type: ignore[arg-type]
            messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
    out.kind, out.error = "failed", "rounds"


async def _prepare(actions: list[Action], already: list[str] | None = None) -> dict[tuple[str, str], str]:
    """Build every card line. Returns {(tool, args): reason} for the calls that cannot be done;
    the reasons for "nothing to change" also go into `already`."""
    refused = {}
    names: set[str] = set()
    for a in actions:
        if a.tool.prepare is None:
            continue
        try:
            a.prepared = await a.tool.prepare(a.args)
            lines = a.prepared if isinstance(a.prepared, list) else [a.prepared]
            # Two lines for one device or schedule: the second would always fail at send (stale)
            mine = {k for p in lines if (k := getattr(p, "key", None)) is not None}
            if mine & names:
                raise ArgError("this card already changes that device or schedule; put all its changes in one call")
            names |= mine
        except ArgError as e:
            refused[(a.tool.name, _key(a.args))] = str(e)
            if isinstance(e, AlreadyError) and already is not None and str(e) not in already:
                already.append(str(e))
        except Exception as e:  # noqa: BLE001 -- a prepare bug is a refused call, not a crashed turn
            logger.exception("Preparing %s failed", a.tool.name)
            refused[(a.tool.name, _key(a.args))] = f"could not prepare this ({e.__class__.__name__})"
    return refused


STATE_RE = re.compile(r"\b(is|are|was|were)( already| still)? (on|off|paused|active|running)\b", re.IGNORECASE)
GENERIC = {"aircon", "ac", "schedule", "timer", "new", "note", "forget", "recipe"}


def _subjects(actions: list[Action]) -> set[str]:
    """Words naming what the card changes ("office", "bed", "time"), from its line names."""
    names = [p.name for a in actions for p in (a.prepared if isinstance(a.prepared, list) else [a.prepared])
             if getattr(p, "name", None)]
    return {w for n in names for w in re.findall(r"[a-z0-9]+", n.lower()) if w not in GENERIC and len(w) > 1}


def claims(text: str) -> bool:
    """True if the text says something was done or a card exists (only a tool call does that)."""
    return bool(CLAIM_RE.search(text)) or any(
        SHORT_CLAIM_RE.fullmatch(s.strip(" -*_`")) for s in SENTENCE_RE.split(text.replace("**", "")))


def _note(text: str | None, subjects: set[str] = frozenset()) -> str:
    """The model's words next to a card, shown above it: only the sentences that do not claim
    a result or talk about the card (the card says that). A sentence stating the on/off/paused
    state of something on the card is dropped too: before the tap it is either stale or a claim."""
    text = (text or "").strip()
    if not text or TOOL_MARKUP_RE.search(text):
        return ""

    def about_card(s: str) -> bool:
        return bool(STATE_RE.search(s)) and bool(subjects & set(re.findall(r"[a-z0-9]+", s.lower())))

    keep = [s for s in SENTENCE_RE.split(text)
            if not (claims(s) or NOTE_DROP_RE.search(s.strip()) or about_card(s))]
    text = " ".join(s.strip() for s in keep).strip()
    return text if len(text) <= MAX_NOTE_CHARS else text[:MAX_NOTE_CHARS].rsplit(" ", 1)[0] + "..."


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
