"""Phase 4.2: routing free text to the assistant, per-person limits, chat context (fake LLM, no IR)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from iotbot.bot import handlers as hd
from iotbot.bot.chat import outcome_text
from iotbot.context import assistant_devices
from iotbot.llm import assistant as asst
from iotbot.llm.assistant import Assistant, Outcome
from iotbot.llm.client import LlmClient
from iotbot.llm.loop import TurnResult
from iotbot.result import Actor
from tests.test_ac_service import make_ctx
from tests.test_bot import ME, make_update, replied
from tests.test_llm import STATE, FakeServer, call, completion

ALICE = Actor(ME, "Alice", "llm")


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_assistant(replies, clock=None):
    srv = FakeServer(replies)
    a = Assistant(srv.client(), lambda: ["bed_ac: aircon in room 'bedroom'"], "Asia/Singapore", clock or Clock())
    a.add_tools(lambda actor: [STATE])
    return a, srv


# ---- assistant core ----------------------------------------------------------------------

async def test_prompt_is_stable_and_the_new_message_says_who_and_when():
    clock = Clock()
    a, srv = make_assistant([completion("Hi."), completion("Hi.")], clock)
    await a.handle(ALICE, "hello")
    clock.t += 3600
    await a.handle(Actor(222, "Bob", "llm"), "hey")
    first, second = srv.requests[0]["messages"], srv.requests[1]["messages"]
    assert first[0] == second[0]                                  # cacheable prefix
    assert first[0]["content"].startswith(asst.RULES) and "- bed_ac: aircon in room 'bedroom'" in first[0]["content"]
    week = "Sat 16 Jan, Sun 17 Jan, Mon 18 Jan, Tue 19 Jan, Wed 20 Jan, Thu 21 Jan, Fri 22 Jan"
    assert first[-1]["content"] == f"[From Alice, Fri 15 Jan 2027 16:00; next days: {week}]\nhello"
    assert second[-1]["content"] == f"[From Bob, Fri 15 Jan 2027 17:00; next days: {week}]\nhey"
    assert srv.requests[0]["tools"][0]["function"]["name"] == "get_home_state"


async def test_context_keeps_last_six_and_resets_when_idle():
    clock = Clock()
    a, srv = make_assistant([completion(f"a{i}") for i in range(6)], clock)
    for i in range(4):
        await a.handle(ALICE, f"q{i}")
    def sent(i):
        return [m["content"].split("\n")[-1] for m in srv.requests[i]["messages"][1:]]

    assert sent(3) == ["q0", "a0", "q1", "a1", "q2", "a2", "q3"]
    assert srv.requests[3]["messages"][1]["content"] == "q0"          # history holds the bare text
    await a.handle(ALICE, "q4")
    assert sent(4) == ["q1", "a1", "q2", "a2", "q3", "a3", "q4"]
    clock.t += asst.CONTEXT_IDLE_S + 1
    await a.handle(ALICE, "q5")
    assert sent(5) == ["q5"]


async def test_failed_and_action_turns_are_not_remembered_by_the_core():
    a, _srv = make_assistant([completion(""), completion(calls=[call("get_home_state", {})]), completion("ok")])
    o = await a.handle(ALICE, "first")
    assert (o.kind, o.turn.error) == ("failed", "empty") and a.context(ME) == []
    await a.handle(ALICE, "second")
    assert [m["content"] for m in a.context(ME)] == ["second", "ok"]        # tool rounds not kept


async def test_one_turn_at_a_time_per_person():
    gate = asyncio.Event()
    a, _ = make_assistant([])

    async def slow_chat(messages, tools=None):
        await gate.wait()
        return (await FakeServer([completion("done")]).client().chat(messages))

    a.llm.chat = slow_chat
    first = asyncio.create_task(a.handle(ALICE, "one"))
    await asyncio.sleep(0)
    assert (await a.handle(ALICE, "two")).kind == "busy_you"
    bob = asyncio.create_task(a.handle(Actor(222, "Bob", "llm"), "x"))     # others are not blocked
    await asyncio.sleep(0)
    assert a.in_flight == {ME, 222}
    gate.set()
    assert (await first).kind == "answer" and (await bob).kind == "answer" and a.in_flight == set()


async def test_rate_limit_and_long_messages():
    clock = Clock()
    a, _ = make_assistant([completion("ok")] * asst.RATE_MAX, clock)
    for _ in range(asst.RATE_MAX):
        assert (await a.handle(ALICE, "hi")).kind == "answer"
        clock.t += 1
    o = await a.handle(ALICE, "hi")
    assert o.kind == "limited" and o.wait_s == asst.RATE_WINDOW_S - asst.RATE_MAX
    assert "Try again in 10 min" in outcome_text(o)
    assert (await a.handle(ALICE, "x" * (asst.MAX_INPUT_CHARS + 1))).kind == "too_long"
    clock.t += asst.RATE_WINDOW_S
    a.llm = FakeServer([completion("ok")]).client()
    assert (await a.handle(ALICE, "hi")).kind == "answer"


def test_failure_lines():
    def failed(code):
        return outcome_text(Outcome("failed", turn=TurnResult("failed", error=code)))

    assert "busy right now" in failed("busy") and "not reachable" in failed("down")
    assert "took too long" in failed("timeout") and "more than 10" in failed("too_many")
    assert "could not work that out" in failed("bug") and "could not work that out" in failed("truncated")
    assert outcome_text(Outcome("answer", "a <b> & c")) == "a &lt;b&gt; &amp; c"
    assert outcome_text(Outcome("busy_you")) == "Still working on your last message."


def test_assistant_sees_managed_aircons_only(tmp_path):
    ctx = make_ctx(tmp_path)
    assert assistant_devices(ctx.registry, ctx.ac) == ["bed_ac: aircon in room 'bedroom'",
                                                       "office_ac: aircon in room 'office'"]
    assert assistant_devices(ctx.registry, None) == []


# ---- the Telegram side -------------------------------------------------------------------

def tg_context(ctx):
    bot = SimpleNamespace(send_chat_action=AsyncMock(), edit_message_text=AsyncMock())
    return SimpleNamespace(args=[], error=None, bot=bot, application=SimpleNamespace(bot_data={"ctx": ctx}))


def say(text, uid=ME, chat_type="private"):
    u = make_update(uid)
    u.effective_message.text = text
    u.effective_chat.type = chat_type
    return u


async def test_private_text_gets_the_answer_with_typing(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.assistant.llm = FakeServer([completion("The bedroom AC was last set to cool 24.")]).client()
    handle = ctx.assistant.handle

    async def slow_handle(actor, text):
        await asyncio.sleep(0.01)                       # long enough for "typing..." to go out
        return await handle(actor, text)

    ctx.assistant.handle = slow_handle
    context = tg_context(ctx)
    u = say("is the bedroom ac on?")
    await hd.on_text(u, context)
    assert replied(u) == "The bedroom AC was last set to cool 24."
    context.bot.send_chat_action.assert_called_with(ME, "typing")


async def test_switched_off_assistant_says_so(tmp_path):
    ctx = make_ctx(tmp_path, env={"LLM_ENABLED": "off"})
    assert ctx.assistant is None
    u = say("hello")
    await hd.on_text(u, tg_context(ctx))
    assert "switched off" in replied(u)


async def test_strangers_and_groups_never_reach_the_llm(tmp_path):
    ctx = make_ctx(tmp_path)
    srv = FakeServer([])
    ctx.assistant.llm = srv.client()
    for u in (say("hi", uid=999), say("hi", chat_type="group"), say("hi", chat_type="supergroup")):
        await hd.on_text(u, tg_context(ctx))
        assert not u.effective_message.reply_text.called
    assert srv.requests == []


async def test_llm_slot_minus_one_means_any_slot(tmp_path):
    ctx = make_ctx(tmp_path, env={"LLM_SLOT": "-1"})
    assert isinstance(ctx.assistant.llm, LlmClient)


def test_build_context_pins_slot(tmp_path, monkeypatch):
    from iotbot import context as c
    seen = []
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    monkeypatch.setattr(c, "LlmClient", lambda url, slot: seen.append((url, slot)) or SimpleNamespace())
    make_ctx(tmp_path / "a")
    make_ctx(tmp_path / "b", env={"LLM_SLOT": "-1"})
    assert seen == [("http://127.0.0.1:8001", 1), ("http://127.0.0.1:8001", None)]


# ---- review follow-ups -------------------------------------------------------------------

async def test_pc_down_or_busy_does_not_use_up_the_limit():
    a, _ = make_assistant([])
    def off(request):
        raise httpx.ConnectError("off")

    a.llm = LlmClient("http://llm", 1, httpx.AsyncClient(transport=httpx.MockTransport(off)))
    for _ in range(asst.RATE_MAX + 5):
        assert (await a.handle(ALICE, "hi")).turn.error == "down"
    assert len(a.recent[ME]) == 0


def wizard_waiting(context, chat_id=ME):
    """Start a draft that waits for a typed time in `chat_id`."""
    w = hd.wizard_of(context)
    d = w.new_draft(ME, device="bed_ac")
    d.step, d.chat_id = "time", chat_id
    w.awaiting[ME] = d.id
    return w, d


async def test_expired_wait_lets_the_text_reach_chat(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.assistant.llm = FakeServer([completion("Hi.")]).client()
    context = tg_context(ctx)
    w, d = wizard_waiting(context)
    d.expires = 0                                         # draft expired, wait never cleared
    u = say("8:30")
    await hd.on_text(u, context)
    assert replied(u) == "Hi." and ME not in w.awaiting


async def test_group_text_neither_answers_nor_ends_a_private_wait(tmp_path):
    ctx = make_ctx(tmp_path)
    context = tg_context(ctx)
    w, d = wizard_waiting(context)
    for text in ("7pm?", "lol"):
        u = say(text, chat_type="group")
        u.effective_chat.id = -100
        await hd.on_text(u, context)
        assert not u.effective_message.reply_text.called
    assert w.awaiting[ME] == d.id


async def test_answer_is_escaped_and_turn_is_noted(tmp_path):
    from iotbot import telemetry
    from iotbot.store import JsonlLog
    from iotbot.telemetry import Tracker

    ctx = make_ctx(tmp_path)
    ctx.assistant.llm = FakeServer([completion("24 < 25 & cool")]).client()
    u = say("compare")
    async with Tracker(JsonlLog(tmp_path / "t", "turns")).turn(surface="text") as t:
        await hd.on_text(u, tg_context(ctx))
        assert telemetry.current() is t
    assert replied(u) == "24 &lt; 25 &amp; cool"
    assert (t.intent, t.outcome, t.tokens) == ("llm:answer", "ok", 920) and t.llm_ms is not None


async def test_two_messages_at_once_one_waits(tmp_path):
    ctx = make_ctx(tmp_path)
    gate = asyncio.Event()

    async def slow_chat(messages, tools=None):
        await gate.wait()
        return await FakeServer([completion("done")]).client().chat(messages)

    ctx.assistant.llm = FakeServer([]).client()
    ctx.assistant.llm.chat = slow_chat
    context = tg_context(ctx)
    u1, u2 = say("one"), say("two")
    first = asyncio.create_task(hd.on_text(u1, context))
    await asyncio.sleep(0.01)
    await hd.on_text(u2, context)
    assert replied(u2) == "Still working on your last message."
    gate.set()
    await first
    assert replied(u1) == "done"


async def test_typing_stops_after_the_reply(tmp_path, monkeypatch):
    from iotbot.bot import chat as ch

    monkeypatch.setattr(ch, "TYPING_EVERY_S", 0.01)
    ctx = make_ctx(tmp_path)
    handle = ctx.assistant.handle
    ctx.assistant.llm = FakeServer([completion("ok")]).client()

    async def slow_handle(actor, text):
        await asyncio.sleep(0.05)
        return await handle(actor, text)

    ctx.assistant.handle = slow_handle
    context = tg_context(ctx)
    await hd.on_text(say("hi"), context)
    n = context.bot.send_chat_action.call_count
    assert n >= 2
    await asyncio.sleep(0.05)
    assert context.bot.send_chat_action.call_count == n


def test_answers_lose_echoed_stamps_and_markdown():
    assert asst.clean_answer("[From Alice, Wed 14 Oct 2026 20:00]\nWhich room?") == "Which room?"
    assert asst.clean_answer("[From Alice, Wed 14 Oct 2026 20:00; next days: Thu 15 Oct, Fri 16 Oct]\n"
                             "[Alice's notes: x]\nWhich room?") == "Which room?"
    assert asst.clean_answer("Two:\n* **Office**: off\n  - bed on") == "Two:\n- Office: off\n- bed on"
    assert asst.clean_answer("Two:\n\n- a") == "Two:\n\n- a"                   # blank lines stay


async def test_an_answer_that_is_only_a_stamp_is_a_failure():
    a, _ = make_assistant([completion("[From Alice, Wed 14 Oct 2026 20:00]")])
    o = await a.handle(ALICE, "hi")
    assert (o.kind, o.turn.error) == ("failed", "empty") and a.context(ME) == []
    assert len(a.recent[ME]) == 0                                            # not counted


async def test_an_answer_claiming_an_action_is_nudged_then_refused():
    for fake in ("Card shown: Run recipe \"good night\"", "Do this?\n- bed: off",
                 "I will turn off the bedroom aircon now.\n\n[Card: Confirm action]", "Done, the office AC is now off.",
                 "Turning the bedroom off for you", "I have noted that you sleep at 23:00.", "I'll remember that."):
        a, srv = make_assistant([completion(fake), completion(fake)])
        o = await a.handle(ALICE, "off the bedroom ac")
        assert (o.kind, o.turn.error) == ("failed", "fake_card"), fake
        assert "no card exists" in srv.requests[1]["messages"][-1]["content"]
        assert "could not work that out" in outcome_text(o) and a.context(ME) == []
    a, _ = make_assistant([completion("I will turn it off."), completion("Which aircon?")])
    o = await a.handle(ALICE, "off it")
    assert (o.kind, o.text, o.turn.tool_calls) == ("answer", "Which aircon?", ["claim!"])
    for fine in ("The office AC is already off.", "The bedroom aircon was last set by the bot to ON cool 24C.",
                 "The office aircon is now off (last set by the bot).", "The card asked to turn off the office.",
                 "No, a credit card cannot pay for that.", "I remember that you sleep at 23:00."):
        a, _ = make_assistant([completion(fine)])
        assert (await a.handle(ALICE, "x")).kind == "answer", fine


@pytest.mark.parametrize("text,out", [
    ("You have a schedule (sf81d) that turns it on.", "You have a schedule that turns it on."),
    ("1. s9a4a: Turns the bedroom off.\n2. s537d: on at 7", "1. Turns the bedroom off.\n2. on at 7"),
    ("the 07:00 on (daily, id sf81d) is paused", "the 07:00 on (daily) is paused"),
    ("Set 5 stars and seed sabe", "Set 5 stars and seed sabe"),            # no digit: not an id
])
def test_schedule_ids_never_reach_the_person(text, out):
    assert asst.clean_answer(text) == out
