"""One assistant turn: model -> read tools -> model ... until an answer or a write (Phase 4.1).

Bounds: at most MAX_TOOL_ROUNDS rounds of tool calls (a write on the call after them is still
accepted: it needs no further round) and TURN_TIMEOUT_S for the whole turn.
A WRITE call makes the card: code, not the model, writes the reply, because in the 4.0 spike
the model claimed "Both aircons are now off" while the card was pending. The card is final
unless the message also asks something (reads run, the model answers) or another change
(LATER_WRITES, a device ordered but not on it): then it waits a round, and only writes the
message asks for join it; its lines never change.
Text the model writes next to its write calls is kept as a short note above the card (the
answer to "... and also check X"), unless it claims anything was done. A plain answer that
claims an action, or promises to skip cards, ends the turn as failed (no retry).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from iotbot.llm.client import LlmClient, LlmError, Reply
from iotbot.llm.tools import ORDER_ALL_RE, AlreadyError, ArgError, Tool, device_kinds, message_text, parse_args

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
    r"\bturning (it |the \w+ )?(on|off) (now|for you)\b|"
    r"(?<!may )(?<!might )(?<!could )\b(has|have)( now)? been (turned|set|switched|changed|paused|resumed|deleted|"
    r"removed|added|created|scheduled)\b|"
    r"\b(i've|i have|done|ok)[,.!]? .{0,40}\bis now (on|off|set)\b|"
    r"\b(i've|i have|i will|i'll) (now )?(noted|note|saved|save|remembered|remember|stored|store)\b|"
    r"\bnoted that\b", re.IGNORECASE)
# live 2026-11-01..15: "I have turned off the office aircon." with no tool call. Per sentence, never
# in a question or a condition ("Should I set it to 23?", "If I set it ...")
FIRST_PERSON_RE = re.compile(
    r"\b(i|i've|i have|i just|we)( also| just| now| already| successfully| gone ahead and)? (turned|switched|set|"
    r"changed|removed|deleted|added|paused|resumed|cancelled|executed)\b", re.IGNORECASE)
CONDITION_RE = re.compile(r"\?\s*$|^\W*(if|when|should|shall|can|could|would|do|does|whether)\b", re.IGNORECASE)
# A promise to stop asking (live 2026-11-10: "I will execute commands immediately without asking
# for confirmation"): only code decides when a card shows
PROMISE_RE = re.compile(r"\b(i will|i'll|from now on|okay, from now on)\b.{0,60}\b(without (asking|confirm\w*|a card)|"
                        r"skip(ping)? (the )?(cards?|confirm\w*|asking)|no longer ask|not ask)\b", re.IGNORECASE)
# A state stated as fact ("The office aircon is now on."): only true after a tool said so. Checked
# only when no tool ran this turn (live 2026-11-15: six of these, nothing was sent)
STATE_CLAIM_RE = re.compile(r"\b(is|are) now (on|off|running|cooling|set to)\b|"
                            r"\b(was|were) (just )?(turned|switched) (on|off)\b", re.IGNORECASE)
# "Remember X" answered with "Got it, I'll use X" and no remember call: nothing was saved (live
# 2026-11-17 20:30, twice). Checked only when the message asks to remember and no write ran
MEMORY_ASK_RE = re.compile(r"^\W*(pls |please |can you |could you )?(remember|note|save|keep in mind)\b", re.IGNORECASE)
ACK_RE = re.compile(r"\b(got it|noted|understood|will do|i'll|i will|i've|i have|from now on)\b", re.IGNORECASE)
DEVICE_WORD_RE = re.compile(r"\b(aircons?|air[- ]?cons?|a/?c|acs|tvs?|television|telly|it|they|both)\b",
                            re.IGNORECASE)
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
    r"\b(i|i've|i have|i'll|i will|i've just|i just|we) (have |will |just |now |also )?(added|created|set up|set|"
    r"scheduled|paused|resumed|deleted|removed|changed|made|turned|switched|updated)\b|"
    r"\b(is|are|has been|have been|will be) (set to |being |now )?(added|created|set up|scheduled|"
    r"deleted|removed|changed|updated|turn(ed)? (on|off)|switch(ed)? (on|off))\b|"
    r"^\W*(ok|okay|sure|got it|here you go|here it is|-)?\W*$", re.IGNORECASE)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
# A sentence about something that is not there or cannot be done
MISSING_RE = re.compile(r"\b(no|not|none|isn't|aren't|doesn't|don't|cannot|can't|couldn't|nothing)\b", re.IGNORECASE)
# A write after the card is ready joins it only when the message asks for that kind of change
# (live 2026-11-14/16: "turn off X and remove the timer" lost the delete: it needs list_schedules
# first, so it always comes a round after the set_device). Never another set_device
LATER_WRITES = {
    "delete_schedule": re.compile(r"\b(delete|remove|cancel|clear|get rid)\b", re.IGNORECASE),
    "pause_schedule": re.compile(r"\bpause\b", re.IGNORECASE),
    "resume_schedule": re.compile(r"\b(resume|unpause)\b", re.IGNORECASE),
    "add_schedule": re.compile(r"\b(add|create|make|new|set( up)?|schedule)\b.{0,30}\b(timers?|schedules?)\b|"
                               r"\b(timers?|schedules?)\b.{0,15}\b(at|in|for)\s+\d|"
                               r"\bin \d+\s*(min|mins|minutes|h|hrs?|hours?)\b|"
                               r"\b(on|off)\b.{0,30}\b(at|by)\s+\d|\b(on|off)\b.{0,30}\b\d{3,4}\b", re.IGNORECASE),
    "edit_schedule": re.compile(r"\b(change|move|edit|update|shift|push|delay)\b.{0,25}\b(timers?|schedules?)\b",
                                re.IGNORECASE),
}
READY = ("ready: shown to the person after your reply, for them to confirm. Do not call it again. If the "
         "message also asks for another change (e.g. remove a timer), call that tool now and it joins this "
         "card. If it asks or checks something, answer that in one or two short sentences (use read tools "
         "if needed), without saying anything is done; if nothing else was asked, reply with just: -")
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
    forced: list[Any] = field(default_factory=list)        # already-set lines, for [Send anyway]
    reads: list[tuple[str, dict[str, Any]]] = field(default_factory=list)   # read tools run: kept in history
    exchanges: list[dict[str, Any]] = field(default_factory=list)

    def note(self, reply: Reply) -> None:
        self.rounds += 1
        self.llm_ms += reply.ms
        self.prompt_tokens += reply.prompt_tokens or 0
        self.completion_tokens += reply.completion_tokens or 0
        self.model = reply.model or self.model
        self.exchanges.append(reply.exchange)


async def run_turn(llm: LlmClient, messages: list[dict[str, Any]], tools: list[Tool],
                   timeout_s: float = TURN_TIMEOUT_S, check_slot: bool = True, ask_rest: bool = False,
                   plain: bool = False) -> TurnResult:
    """`messages` = system + context + the new user message. Tool rounds go into a copy, so
    the caller's list never holds half a round (tool calls without results). `ask_rest`: the
    message looks like it also asks something, so a card made without any read gets one more
    model call for that answer. `plain`: the message asks nothing besides the change, so a card
    shows no note from the model. Never raises."""
    out = TurnResult("failed")
    try:
        async with asyncio.timeout(timeout_s):
            if check_slot:
                await llm.check_slot()
            await _loop(llm, list(messages), tools, out, ask_rest, plain)
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
                ask_rest: bool = False, plain: bool = False) -> None:
    by_name = {t.name: t for t in tools}
    specs = [t.spec() for t in tools]
    already: list[str] = []                      # refused because nothing would change: the card says so
    pending: list[Action] | None = None          # a ready card, waiting for the model's answer to the rest
    had_reads = False                            # a check was looked up in an earlier round
    not_on: dict[tuple[str, str], str] = {}      # later writes that could not join the card: said below it
    told = False                                 # a write was refused with a reason ("bed is off"): state came from code
    repeats: set[str] = set()                    # call ids that only repeat a line already on the card

    def card(actions: list[Action], text: str | None) -> None:
        out.kind, out.actions, out.error = "actions", actions, None
        # Words above a card only answer what else the message asked; a plain change gets none
        # (live: "pause the 7am one" got "...has been paused. It will resume automatically ...")
        note = _note(text, _subjects(actions), tools_ran=had_reads or told)
        if plain:
            # Only what is missing or left out ("There is no bedroom timer at 10:41"), so a card that
            # covers part of the message does not read as the whole of it
            note = " ".join(s for s in SENTENCE_RE.split(note) if MISSING_RE.search(s))
        out.text = note or ("Left out: " + "; ".join(already) + "." if already else "")
        if not_on:
            out.text = (out.text + "\n" if out.text else "") + (
                "Could not add: " + "; ".join(dict.fromkeys(not_on.values())) + ". Ask for it again after this.")

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
            if PROMISE_RE.search(reply.content):
                out.tool_calls.append("claim!")
                out.kind, out.error = "failed", "promise"
                return
            if claims(reply.content, tools_ran=had_reads or told) or (
                    MEMORY_ASK_RE.search(message_text.get()) and ACK_RE.search(reply.content)):
                # Text that announces an action or a card, without the tool call: no card exists.
                # No retry: at temperature 0 the model repeats itself (live 2 of 2, eval 0 of 18 rescued)
                out.tool_calls.append("claim!")
                out.kind, out.error = "failed", "fake_card"
                return
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
            # The card is ready: its lines never change. A write the message asked for joins it
            # (prepared on its own, so a line already promised is never re-planned or swapped)
            said = message_text.get()
            ordered = _uncovered(_devices_ordered(said, tools), pending)
            for c in writes:
                gate = LATER_WRITES.get(c.tool.name)               # type: ignore[union-attr]
                if c.tool.name in ("set_device", "set_device_now") \
                        and (c.args or {}).get("device") in ordered:  # type: ignore[union-attr]
                    continue                                       # "off bedroom and on office": ordered, not on the card
                if gate is None or not gate.search(said):
                    c.error = "the card is already made; do not call write tools again, answer in text"
            joining, writes = [c for c in writes if not c.error], []
            if joining and not bad:
                promised = {(a.tool.name, _key(a.args)) for a in pending}
                new: list[Action] = []
                for c in joining:
                    a = Action(c.tool, c.args)                     # type: ignore[arg-type]
                    if (c.tool.name, _key(c.args)) not in promised and a not in new:   # type: ignore[union-attr]
                        new.append(a)                              # a repeat of a card line just gets "ready"
                no_op: set[tuple[str, str]] = set()
                refused = await _prepare(new, already, {k for a in pending for p in _lines(a)
                                                        if (k := getattr(p, "key", None)) is not None}, no_op,
                                         out.forced)
                hard = {k: v for k, v in refused.items() if k not in no_op}   # "already paused" is on Left out
                lines = sum(len(_lines(a)) for a in pending + new if (a.tool.name, _key(a.args)) not in refused)
                if not hard and lines > MAX_WRITES:
                    hard = {(a.tool.name, _key(a.args)): f"a card holds at most {MAX_WRITES} changes"
                            for a in new if (a.tool.name, _key(a.args)) not in refused}
                for c in joining:
                    k = (c.tool.name, _key(c.args))                # type: ignore[union-attr]
                    if k in promised:
                        repeats.add(c.id)                          # already a card line: told so, nothing added
                    elif hard:
                        # All or none: a card that holds half of a later request would read as all of it
                        c.error = hard.get(k) or refused.get(k) or "not added: another change in this round could not be made"
                    elif k in refused:
                        c.error = refused[k]
                if hard:
                    not_on.update(hard)                            # kept: a later fix cannot tell which one it fixed
                else:
                    pending = pending + [a for a in new if (a.tool.name, _key(a.args)) not in refused]
                    ready = True
                card(pending, None)
            out.tool_calls = out.tool_calls[:-len(checked)] + [
                (c.tool.name if c.tool else "?") + ("!" if c.error else "") for c in checked]
        if writes and not bad:
            actions: list[Action] = []
            for c in writes:
                a = Action(c.tool, c.args)                     # type: ignore[arg-type]
                if a not in actions:                           # the same call twice is one card line
                    actions.append(a)
            if len(actions) > MAX_WRITES:
                out.kind, out.error = "failed", "too_many"
                return
            refused = await _prepare(actions, already, forced=out.forced)
            told = told or bool(refused)
            # A card with nothing else asked (or no rounds left) is the reply now
            # The message also asks for another kind of change ("... and remove the timer"): that one
            # needs list_schedules first, so the card waits a round for it (eval 2026-11-17)
            # So does one that names a device the card leaves out ("off bedroom at 2:30 and on office")
            said = message_text.get()
            more = any(g.search(said) for name, g in LATER_WRITES.items()
                       if name in by_name and name not in {a.tool.name for a in actions}) \
                or bool(_uncovered(_devices_ordered(said, tools), actions))
            asked = reads or more or ((had_reads or ask_rest) and not _note(reply.content, _subjects(actions)))
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
            elif c.id in repeats:
                result = _dump({"card": "already on this card; do not call it again"})
            elif c.tool.write and ready:                       # type: ignore[union-attr]
                result = _dump({"card": READY})
            elif c.tool.write:                                 # type: ignore[union-attr]
                result = _dump({"error": "not done: fix the other calls in this round first"})
            else:
                result = await _run_read(c.tool, c.args)        # type: ignore[arg-type]
                out.reads.append((c.tool.name, c.args))         # type: ignore[union-attr]
            messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
    out.kind, out.error = "failed", "rounds"


QUESTION_CLAUSE_RE = re.compile(r"^\s*(and |but )?(is|are|was|were|what|what's|which|when|how|does|do|did|"
                                r"has|have)\b", re.IGNORECASE)


TV_WORDS = {"tv", "tvs", "television", "telly"}
AC_WORDS = {"aircon", "aircons", "ac", "acs", "air"}
FILLER = {"the", "my", "too", "also", "pls", "please", "plz", "con"}


def _kind(d: str) -> str:
    known = device_kinds.get().get(d)
    return known or ("tv" if TV_WORDS & set(re.findall(r"[a-z0-9]+", d.lower())) else "ac")


def _rooms(d: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", d.lower())
            if w not in GENERIC | TV_WORDS | AC_WORDS | {"room"} and len(w) > 2}


def _uncovered(ordered: set[str], actions: list[Action]) -> set[str]:
    """Ordered devices the card does not change yet. A card line covers its device id, or a device
    of the same kind and room ("office" covers office-aircon, never bedroom-tv for bedroom-aircon)."""
    on_card = [str(a.args.get("device", "")) for a in actions if a.args.get("device")]
    # Lines with no device argument (edit/pause/delete a schedule, a recipe) cover the devices their
    # names mention ("schedule Office off" covers office-aircon), as before Phase 10
    named = _subjects([a for a in actions if not a.args.get("device")])
    return {d for d in ordered if d not in on_card and not any(
        _kind(c) == _kind(d) and _rooms(c) and _rooms(c) <= _rooms(d) for c in on_card)
        and not (_rooms(d) and _rooms(d) <= named and (_kind(d) == "tv") == bool(TV_WORDS & named))}


def _named(said: set[str], ids: set[str]) -> set[str]:
    """Device ids a clause names, by room and kind: "bedroom tv" is bedroom-tv, "bedroom aircon" and
    a bare "bedroom" are bedroom-aircon (a TV only when named), "the tv" is the only TV."""
    kind, rooms = _kind, _rooms
    wants = {k for k, words in (("tv", TV_WORDS), ("ac", AC_WORDS)) if words & said} or {"ac"}
    out = set()
    for k in wants:
        same = [d for d in ids if kind(d) == k]
        by_room = {d for d in same if rooms(d) & said}       # any word, as in Phase 9 ("off master")
        if by_room:
            out |= by_room
        elif len(same) == 1 and (k == "tv" or AC_WORDS & said):
            out |= set(same)                              # "the tv" / "the aircon": the only one
    return out


def _devices_ordered(text: str, tools: list[Tool]) -> set[str]:
    """Device ids that a clause orders on, off or at a time: "and on office at 1330" counts;
    "bedroom is fine" and "is bedroom on?" do not. A bare list after an order ("off bedroom
    aircon and tv") belongs to it."""
    ids = {d for t in tools for d in t.parameters.get("properties", {}).get("device", {}).get("enum", [])}
    out: set[str] = set()
    ordering = False
    all_rooms = {w for d in ids for w in _rooms(d)}
    room_said: set[str] = set()                       # "off bedroom tv and aircon": the aircon is the bedroom's
    for clause in re.split(r"[,;.?!\n]|\band\b|\bthen\b", text):
        if not clause.strip() or QUESTION_CLAUSE_RE.match(clause):
            ordering = False
            continue
        said = set(re.findall(r"[a-z0-9]+", clause.lower()))
        if said & all_rooms:
            room_said = said & all_rooms
        elif ordering and (TV_WORDS | AC_WORDS) & said:
            said |= room_said
        if {"on", "off"} & said or re.search(r"\b(at|by)\s+\d|\b\d{3,4}\b", clause):
            ordering = True
            out |= ids if ORDER_ALL_RE.search(clause) else _named(said, ids)   # "turn everything off"
        elif ordering and not said - FILLER - TV_WORDS - AC_WORDS - GENERIC - {
                w for d in ids for w in re.findall(r"[a-z0-9]+", d.lower())}:
            out |= _named(said, ids)                      # only device words: "... and (the) tv"
        else:
            ordering = False
    return out


def _lines(a: Action) -> list[Any]:
    return a.prepared if isinstance(a.prepared, list) else [a.prepared]


async def _prepare(actions: list[Action], already: list[str] | None = None,
                   taken: set[str] | None = None, no_op: set[tuple[str, str]] | None = None,
                   forced: list[Any] | None = None) -> dict[tuple[str, str], str]:
    """Build every card line. Returns {(tool, args): reason} for the calls that cannot be done;
    the reasons for "nothing to change" also go into `already`. `taken`: what the card already
    changes (a later round joining it)."""
    refused = {}
    names: set[str] = set(taken or ())
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
            if isinstance(e, AlreadyError):
                if already is not None and e.short not in already:
                    already.append(e.short)                      # the short form: it goes on the card
                if no_op is not None:
                    no_op.add((a.tool.name, _key(a.args)))
                if forced is not None and e.force is not None and all(
                        getattr(f, "key", None) != getattr(e.force, "key", None) for f in forced):
                    forced.append(e.force)
        except Exception as e:  # noqa: BLE001 -- a prepare bug is a refused call, not a crashed turn
            logger.exception("Preparing %s failed", a.tool.name)
            refused[(a.tool.name, _key(a.args))] = f"could not prepare this ({e.__class__.__name__})"
    return refused


STATE_RE = re.compile(r"\b(is|are|was|were)( already| still)?( not)? (on|off|paused|active|running)\b(?!-)",
                      re.IGNORECASE)
IF_BEFORE_RE = re.compile(r"\b(if|whether|without)\b", re.IGNORECASE)
GENERIC = {"aircon", "ac", "schedule", "timer", "new", "note", "forget", "recipe"}


def _subjects(actions: list[Action]) -> set[str]:
    """Words naming what the card changes ("office", "bed", "time"), from its line names."""
    names = [p.name for a in actions for p in (a.prepared if isinstance(a.prepared, list) else [a.prepared])
             if getattr(p, "name", None)]
    return {w for n in names for w in re.findall(r"[a-z0-9]+", n.lower()) if w not in GENERIC and len(w) > 1}


def claims(text: str, tools_ran: bool = True) -> bool:
    """True if the text says something was done or a card exists (only a tool call does that).
    `tools_ran` False: no tool ran this turn, so a device's state stated as fact is a claim too
    ("The office aircon is now on."), while a state answer after a read stays fine."""
    sentences = SENTENCE_RE.split(text.replace("**", ""))
    said = [s for s in sentences if not CONDITION_RE.search(s)]           # statements, not questions or ifs
    if CLAIM_RE.search(text) or any(SHORT_CLAIM_RE.fullmatch(s.strip(" -*_`")) for s in sentences) \
            or any(FIRST_PERSON_RE.search(s) for s in said):
        return True
    def stated(s: str) -> bool:
        m = STATE_CLAIM_RE.search(s) or (DEVICE_WORD_RE.search(s) and STATE_RE.search(s))
        return bool(m) and not IF_BEFORE_RE.search(s[:m.start()])     # "I cannot tell if it is on"
    return not tools_ran and any(stated(s) for s in said)


def _note(text: str | None, subjects: set[str] = frozenset(), tools_ran: bool = True) -> str:
    """The model's words next to a card, shown above it: only the sentences that do not claim
    a result or talk about the card (the card says that). A sentence stating the on/off/paused
    state of something on the card is dropped too: before the tap it is either stale or a claim."""
    text = (text or "").strip()
    if not text or TOOL_MARKUP_RE.search(text):
        return ""

    def about_card(s: str) -> bool:
        return bool(STATE_RE.search(s)) and bool(subjects & set(re.findall(r"[a-z0-9]+", s.lower())))

    keep = [s for s in SENTENCE_RE.split(text)
            if not (claims(s, tools_ran) or NOTE_DROP_RE.search(s.strip()) or about_card(s))]
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
