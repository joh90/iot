"""Phase 4.3: confirm cards, Undo and Retry (fake devices, no IR)."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from iotbot.bot import handlers as hd
from iotbot.llm import cards as cd
from iotbot.llm.cards import CardError, Cards, Done, Prepared
from iotbot.llm.tools import ArgError, Tool
from iotbot.result import Actor
from tests.test_ac_service import make_ctx
from tests.test_bot import ME, make_update, replied
from tests.test_chat import Clock, say, tg_context
from tests.test_llm import FakeServer, call, completion

ALICE = Actor(ME, "Alice", "llm")


class Thing:
    """A fake device: a value, a version, and a log of what was sent."""

    def __init__(self, name, value="off", fail=False):
        self.name, self.value, self.version, self.fail, self.sent = name, value, 0, fail, []

    def set(self, value):
        self.value, self.version = value, self.version + 1

    def item(self, to):
        before = self.value

        async def run(actor):
            if self.fail:
                return Done(False, "no reply from remote")
            self.sent.append(to)
            self.set(to)
            mine = self.version

            async def undo(actor):
                if self.version != mine:
                    return Done(False, "something newer was sent")
                self.sent.append(before)
                self.set(before)
                return Done(True, "ok")

            return Done(True, "ok", undo)

        return Prepared(self.name, f"{before} -> {to}", run, lambda: self.version)


async def act(cards, card, op):
    cards.begin(card, op)
    return await cards.run(card, op, ALICE)


async def test_do_then_undo():
    a, b = Thing("bed"), Thing("office", "on")
    cards = Cards(Clock())
    card, _ = cards.new(ME, [a.item("cool 23"), b.item("off")])
    await act(cards, card, "do")
    assert (a.value, b.value, card.status) == ("cool 23", "off", "done")
    with pytest.raises(CardError, match="Nothing failed"):
        cards.begin(card, "retry")
    with pytest.raises(CardError, match="Already done"):
        cards.begin(card, "do")
    undone = await act(cards, card, "undo")
    assert [u.ok for u in undone] == [True, True] and (a.value, b.value) == ("off", "on")
    assert card.status == "undone"
    with pytest.raises(CardError):
        cards.begin(card, "undo")


async def test_changed_since_drawn_runs_nothing():
    a = Thing("bed")
    cards = Cards(Clock())
    card, _ = cards.new(ME, [a.item("cool 23")])
    a.set("cool 25")                                       # someone used /keyboard meanwhile
    with pytest.raises(CardError, match="bed changed since"):
        cards.begin(card, "do")
    assert a.sent == [] and card.status == "expired"


async def test_expiry_cancel_and_newest_card_wins():
    clock = Clock()
    cards = Cards(clock)
    a = Thing("bed")
    first, _ = cards.new(ME, [a.item("x")])
    second, replaced = cards.new(ME, [a.item("y")])
    assert replaced == [first] and first.status == "replaced"
    with pytest.raises(CardError, match="Replaced"):
        cards.begin(first, "do")
    other, replaced = cards.new(222, [a.item("z")])          # other people keep their cards
    assert replaced == [] and second.status == "pending"
    cards.cancel(other)
    with pytest.raises(CardError, match="cancelled"):
        cards.begin(other, "do")
    clock.t += cd.CARD_TTL_S + 1
    with pytest.raises(CardError, match="expired"):
        cards.begin(second, "do")
    assert a.sent == []


async def test_other_people_cannot_use_my_card():
    cards = Cards(Clock())
    card, _ = cards.new(ME, [Thing("bed").item("x")])
    with pytest.raises(CardError, match="no longer active"):
        cards.get(card.id, 222)


async def test_partial_failure_retry_and_undo_only_what_worked():
    a, b = Thing("bed"), Thing("office", "on", fail=True)
    clock = Clock()
    cards = Cards(clock)
    card, _ = cards.new(ME, [a.item("cool 23"), b.item("off")])
    await act(cards, card, "do")
    assert [r.ok for r in card.results] == [True, False]
    await act(cards, card, "retry")                         # still failing
    b.fail = False
    await act(cards, card, "retry")
    assert [r.ok for r in card.results] == [True, True] and a.sent == ["cool 23"] and b.sent == ["off"]
    a.set("cool 26")                                        # newer than our send: that line refuses
    undone = await act(cards, card, "undo")
    assert [u.ok for u in undone] == [False, True] and a.value == "cool 26" and b.value == "on"
    assert card.status == "done"                            # bed can still be tried again
    undone = await act(cards, card, "undo")
    assert undone[1] is None and b.sent == ["off", "on"]    # office is not undone twice
    clock.t += 1
    card2, _ = cards.new(ME, [a.item("x")])
    await act(cards, card2, "do")
    clock.t += cd.UNDO_TTL_S + 1
    with pytest.raises(CardError, match="Too late"):
        cards.begin(card2, "undo")


async def test_retry_skips_a_line_that_changed_since():
    b = Thing("office", "on", fail=True)
    cards = Cards(Clock())
    card, _ = cards.new(ME, [b.item("off")])
    await act(cards, card, "do")
    b.fail = False
    b.set("cool 22")                                        # /keyboard after the failed Do
    await act(cards, card, "retry")
    assert card.results[0].text == "changed since, not sent again" and b.sent == []


async def test_double_tap_runs_once():
    a = Thing("bed")
    gate = asyncio.Event()
    item = a.item("cool 23")
    run = item.run

    async def slow(actor):
        await gate.wait()
        return await run(actor)

    item.run = slow
    cards = Cards(Clock())
    card, _ = cards.new(ME, [item])
    first = asyncio.create_task(act(cards, card, "do"))
    await asyncio.sleep(0)
    for op in ("do", "undo", "retry"):
        with pytest.raises(CardError):
            cards.begin(card, op)
    gate.set()
    await first
    assert a.sent == ["cool 23"]


async def test_cancelled_do_keeps_what_ran():
    a, b = Thing("bed"), Thing("office")
    gate = asyncio.Event()
    second = b.item("off")

    async def hang(actor):
        await gate.wait()

    second.run = hang
    cards = Cards(Clock())
    card, _ = cards.new(ME, [a.item("cool 23"), second])
    task = asyncio.create_task(act(cards, card, "do"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert card.status == "done" and card.results[0].ok and "interrupted" in card.results[1].text
    undone = await act(cards, card, "undo")
    assert undone[0].ok and a.value == "off"


async def test_a_crashing_line_is_a_failed_line():
    async def boom(actor):
        raise RuntimeError("x")

    cards = Cards(Clock())
    card, _ = cards.new(ME, [Prepared("bed", "x", boom, lambda: 0), Thing("office").item("off")])
    await act(cards, card, "do")
    assert [r.ok for r in card.results] == [False, True] and "RuntimeError" in card.results[0].text


def test_old_cards_are_pruned():
    clock = Clock()
    cards = Cards(clock)
    cards.new(ME, [Thing("bed").item("x")])
    clock.t += cd.CARD_TTL_S + cd.UNDO_TTL_S + 1
    cards.new(222, [Thing("bed").item("x")])
    assert len(cards.cards) == 1


# ---- loop: prepare refusals go back to the model -----------------------------------------

async def test_prepare_refusal_goes_back_to_the_model():
    from iotbot.llm import loop as lp

    async def prep(args):
        if args["device"] == "bed":
            raise ArgError("bed is off; ask whether to turn it on")
        return Thing(args["device"]).item("off")

    t = Tool("set", "", {"type": "object", "properties": {"device": {"type": "string"}}}, write=True, prepare=prep)
    srv = FakeServer([completion(calls=[call("set", {"device": "bed"}, "a"), call("set", {"device": "office"}, "b")]),
                      completion("The bedroom AC is off. Turn it on?")])
    out = await lp.run_turn(srv.client(), [{"role": "user", "content": "x"}], [t])
    assert out.kind == "answer" and out.tool_calls == ["set!", "set"]
    results = [m["content"] for m in srv.requests[1]["messages"] if m["role"] == "tool"]
    assert "bed is off" in results[0] and "fix the other calls" in results[1]


# ---- Telegram flow -----------------------------------------------------------------------

def card_ctx(tmp_path, things, replies):
    ctx = make_ctx(tmp_path)

    async def prep(args):
        return things[args["device"]].item(args["to"])

    t = Tool("set", "", {"type": "object", "properties": {"device": {"type": "string"}, "to": {"type": "string"}}},
             write=True, prepare=prep)
    ctx.assistant.add_tools(lambda actor: [t])
    ctx.assistant.llm = FakeServer(replies).client()
    return ctx


def tap(card_id, op, uid=ME):
    u = make_update(uid, data=f"lc:{op}:{card_id}")
    u.callback_query.message = SimpleNamespace(chat_id=uid, message_id=9)
    return u


async def test_card_flow_end_to_end(tmp_path):
    bed = Thing("bed-ac", "ON cool 24C")
    ctx = card_ctx(tmp_path, {"bed": bed}, [completion("Done!", [call("set", {"device": "bed", "to": "OFF"})])])
    context = tg_context(ctx)
    u = say("off the bedroom ac")
    u.effective_message.reply_text = AsyncMock(return_value=SimpleNamespace(chat_id=ME, message_id=9))
    await hd.on_text(u, context)
    (text,), kw = u.effective_message.reply_text.call_args
    assert text.startswith("Do this?\n- <b>bed-ac</b>: ON cool 24C -&gt; OFF") and "Done!" not in text
    assert [b.text for b in kw["reply_markup"].inline_keyboard[0]] == ["Do it", "Cancel"]
    card = next(iter(ctx.assistant.cards.cards.values()))
    hist = ctx.assistant.context(ME)
    assert [m["role"] for m in hist] == ["user", "assistant", "tool"]          # a card is kept as tool calls
    assert hist[1]["tool_calls"][0]["function"] == {"name": "set", "arguments": '{"device": "bed", "to": "OFF"}'}
    assert json.loads(hist[2]["content"]) == {"card": "shown to the person, waiting for their tap",
                                              "lines": ["bed-ac: ON cool 24C -> OFF"]}
    assert bed.sent == []                                         # nothing before the tap

    t = tap(card.id, "do")
    await hd.on_button(t, context)
    t.callback_query.answer.assert_called_once_with("Working on it...")
    (text,), kw = t.callback_query.edit_message_text.call_args
    assert text.startswith("Done (by Alice, ") and "OFF  ok" in text and bed.sent == ["OFF"]
    t = tap(card.id, "do")                                          # double tap after done
    await hd.on_button(t, context)
    t.callback_query.answer.assert_called_once_with("Already done.", show_alert=True)
    assert [b.text for b in kw["reply_markup"].inline_keyboard[0]] == ["Undo"]
    assert json.loads(ctx.assistant.context(ME)[-1]["content"]) == {
        "card": "the person tapped Do it", "lines": ["bed-ac: ON cool 24C -> OFF (ok)"]}

    t = tap(card.id, "undo")
    await hd.on_button(t, context)
    (text,), _ = t.callback_query.edit_message_text.call_args
    assert text.startswith("Undone (by Alice") and bed.sent == ["OFF", "ON cool 24C"]


async def test_note_above_card_and_each_call_keeps_its_own_line(tmp_path):
    bed, office = Thing("bed-ac", "ON cool 24C"), Thing("office-ac", "ON cool 23C")
    ctx = card_ctx(tmp_path, {"bed": bed, "office": office}, [completion(
        "Your 07:00 on schedule is paused.", [call("set", {"device": "bed", "to": "OFF"}, "a"),
                                              call("set", {"device": "office", "to": "OFF"}, "b")])])
    context = tg_context(ctx)
    u = say("off both, and is my 7am paused?")
    u.effective_message.reply_text = AsyncMock(return_value=SimpleNamespace(chat_id=ME, message_id=9))
    await hd.on_text(u, context)
    (text,), _ = u.effective_message.reply_text.call_args
    assert text.startswith("<i>Your 07:00 on schedule is paused.</i>\n\nDo this?\n")
    hist = ctx.assistant.context(ME)
    assert hist[1]["content"] == "Your 07:00 on schedule is paused."
    assert [json.loads(m["content"])["lines"] for m in hist[2:]] == [["bed-ac: ON cool 24C -> OFF"],
                                                                    ["office-ac: ON cool 23C -> OFF"]]
    card = next(iter(ctx.assistant.cards.cards.values()))
    t = tap(card.id, "do")
    await hd.on_button(t, context)
    (text,), _ = t.callback_query.edit_message_text.call_args
    assert text.startswith("<i>Your 07:00 on schedule is paused.</i>\n\nDone (by Alice")


async def test_failed_line_shows_retry_and_cancel_and_stale_taps(tmp_path):
    office = Thing("office-ac", "on", fail=True)
    ctx = card_ctx(tmp_path, {"office": office}, [completion(calls=[call("set", {"device": "office", "to": "OFF"})]),
                                                  completion(calls=[call("set", {"device": "office", "to": "x"})])])
    context = tg_context(ctx)
    await hd.on_text(say("off the office"), context)
    card = next(iter(ctx.assistant.cards.cards.values()))
    t = tap(card.id, "do")
    await hd.on_button(t, context)
    (text,), kw = t.callback_query.edit_message_text.call_args
    assert "Done (0 of 1)" in text and "FAILED (no reply from remote)" in text
    assert [b.text for b in kw["reply_markup"].inline_keyboard[0]] == ["Retry failed"]
    assert "(FAILED (no reply from remote))" in ctx.assistant.context(ME)[-1]["content"]

    await hd.on_text(say("again"), context)
    second = next(c for c in ctx.assistant.cards.cards.values() if c.id != card.id)
    t = tap(second.id, "x")
    await hd.on_button(t, context)
    assert "Cancelled; nothing was done" in t.callback_query.edit_message_text.call_args[0][0]
    t = tap(second.id, "do")
    await hd.on_button(t, context)
    t.callback_query.answer.assert_called_once_with("This was cancelled.", show_alert=True)
    t = tap(card.id, "undo")                                       # nothing worked: refused, one answer
    await hd.on_button(t, context)
    t.callback_query.answer.assert_called_once_with("Nothing here can be undone.", show_alert=True)
    t = tap("nosuch", "do")
    await hd.on_button(t, context)
    t.callback_query.answer.assert_called_with("This button is no longer active.", show_alert=True)
    t = tap(card.id, "do", uid=999)                                # strangers: restricted
    await hd.on_button(t, context)
    assert office.sent == []


async def test_replaced_card_message_is_edited(tmp_path):
    bed = Thing("bed-ac")
    ctx = card_ctx(tmp_path, {"bed": bed}, [completion(calls=[call("set", {"device": "bed", "to": "a"})]),
                                            completion(calls=[call("set", {"device": "bed", "to": "b"})])])
    context = tg_context(ctx)
    for text in ("one", "two"):
        u = say(text)
        u.effective_message.reply_text = AsyncMock(return_value=SimpleNamespace(chat_id=ME, message_id=40))
        await hd.on_text(u, context)
    kw = context.bot.edit_message_text.call_args.kwargs
    assert "Replaced by your newer request" in context.bot.edit_message_text.call_args.args[0]
    assert (kw["chat_id"], kw["message_id"]) == (ME, 40)


async def test_write_without_card_line_never_runs(tmp_path):
    ctx = make_ctx(tmp_path)
    t = Tool("set", "", {"type": "object", "properties": {}}, write=True)
    ctx.assistant.add_tools(lambda actor: [t])
    ctx.assistant.llm = FakeServer([completion(calls=[call("set", {})])]).client()
    u = say("x")
    await hd.on_text(u, tg_context(ctx))
    assert "could not work that out" in replied(u)


async def test_unexpected_prepare_error_goes_back_to_the_model():
    from iotbot.llm import loop as lp

    async def prep(args):
        raise RuntimeError("bug")

    t = Tool("set", "", {"type": "object", "properties": {}}, write=True, prepare=prep)
    srv = FakeServer([completion(calls=[call("set", {})]), completion("Sorry, that did not work.")])
    out = await lp.run_turn(srv.client(), [{"role": "user", "content": "x"}], [t])
    assert (out.kind, out.actions, out.tool_calls) == ("answer", [], ["set!"])
    assert "could not prepare this (RuntimeError)" in srv.requests[1]["messages"][-1]["content"]
