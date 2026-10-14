"""Phase 4.1: llama-server client, strict tool args, the bounded tool loop (fake server, no IR)."""

import asyncio
import json

import httpx
import pytest

from iotbot.llm import loop as lp
from iotbot.llm.client import LlmClient, LlmError, parse_reply
from iotbot.llm.tools import ArgError, Tool, parse_args, schema_problems

SET_AC = Tool("set_ac", "Set an aircon", {
    "type": "object", "required": ["device"], "properties": {
        "device": {"type": "string", "enum": ["bedroom-aircon", "office-aircon"]},
        "power": {"type": "boolean"},
        "temp": {"type": "integer", "minimum": 16, "maximum": 31},
        "at": {"type": "string", "pattern": "[0-2][0-9]:[0-5][0-9]"},
        "days": {"type": "array", "maxItems": 7, "items": {"type": "string", "enum": ["mon", "tue"]}}}},
    write=True)


async def _state(args):
    return {"bedroom-aircon": "on, cool 24"}


STATE = Tool("get_home_state", "State", {"type": "object", "properties": {}}, run=_state)


def call(name, args, cid="c1"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}


def completion(content="", calls=None, model="qwen-test"):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = calls
    return {"model": model, "choices": [{"message": msg}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 20}, "timings": {"cache_n": 880}}


class FakeServer:
    """Answers /slots and /v1/chat/completions from a script of replies; records requests."""

    def __init__(self, replies, slots=None):
        self.replies = list(replies)
        self.slots = slots if slots is not None else [{"id": 0, "is_processing": True},
                                                       {"id": 1, "is_processing": False}]
        self.requests = []

    def __call__(self, request):
        if request.url.path == "/slots":
            return self.slots if isinstance(self.slots, httpx.Response) else httpx.Response(200, json=self.slots)
        self.requests.append(json.loads(request.content))
        r = self.replies.pop(0)
        return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)

    def client(self, slot=1):
        return LlmClient("http://llm", slot, httpx.AsyncClient(transport=httpx.MockTransport(self)))


def msgs(text="off the bedroom aircon"):
    return [{"role": "system", "content": "home"}, {"role": "user", "content": text}]


# ---- client ------------------------------------------------------------------------------

async def test_request_pins_slot_and_turns_thinking_off():
    srv = FakeServer([completion("hi")])
    r = await srv.client().chat(msgs(), [STATE.spec()])
    body = srv.requests[0]
    assert body["id_slot"] == 1 and body["cache_prompt"] is True
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["tools"][0]["function"]["name"] == "get_home_state"
    assert (r.content, r.model, r.prompt_tokens, r.cached_tokens) == ("hi", "qwen-test", 900, 880)
    assert r.exchange["request"] == body and r.exchange["ms"] == r.ms


async def test_any_slot_sends_no_id_slot():
    srv = FakeServer([completion("hi")])
    await srv.client(slot=None).chat(msgs())
    assert "id_slot" not in srv.requests[0] and "tools" not in srv.requests[0]


async def test_busy_slot_fails_fast_and_other_slot_busy_is_fine():
    srv = FakeServer([], slots=[{"id": 1, "is_processing": True}])
    with pytest.raises(LlmError) as e:
        await srv.client().check_slot()
    assert e.value.code == "busy"
    await FakeServer([]).client().check_slot()            # slot 0 busy: not ours


@pytest.mark.parametrize("slots", [httpx.Response(404), httpx.Response(200, text="x"),
                                   httpx.Response(200, json={"a": 1}), [{"id": 0}]])
async def test_slot_check_is_skipped_when_unknown(slots):
    await FakeServer([], slots=slots).client().check_slot()


async def test_loading_server_and_refused_connection_are_down():
    with pytest.raises(LlmError) as e:
        await FakeServer([], slots=httpx.Response(503)).client().check_slot()
    assert e.value.code == "down"

    def refuse(request):
        raise httpx.ConnectError("refused")

    c = LlmClient("http://llm", 1, httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    for coro in (c.check_slot(), c.chat(msgs())):
        with pytest.raises(LlmError) as e:
            await coro
        assert e.value.code == "down"


@pytest.mark.parametrize("resp,code", [(httpx.Response(500), "http"), (httpx.Response(200, text="<html>"), "bad_reply"),
                                       (httpx.Response(200, json={"choices": []}), "bad_reply"),
                                       (httpx.Response(200, json={"choices": [{"message": "x"}]}), "bad_reply")])
async def test_broken_server_replies(resp, code):
    with pytest.raises(LlmError) as e:
        await FakeServer([resp]).client().chat(msgs())
    assert e.value.code == code


def test_parse_strips_think_and_fixes_odd_calls():
    r = parse_reply(completion("<think>hmm</think> Done.", [
        {"function": {"name": "set_ac", "arguments": {"device": "office-aircon"}}},
        {"id": "", "function": {"name": "x", "arguments": None}}]), 5, {})
    assert r.content == "Done."
    assert r.tool_calls[0].arguments == '{"device": "office-aircon"}' and r.tool_calls[0].id.startswith("call_")
    assert r.tool_calls[1].arguments == ""
    with pytest.raises(LlmError):
        parse_reply(completion(calls=[{"function": {"arguments": "{}"}}]), 5, {})
    r = parse_reply({"choices": [{"message": {"content": None}}], "usage": {"prompt_tokens": True}}, 5, {})
    assert (r.content, r.prompt_tokens, r.model) == ("", None, None)


# ---- strict args -------------------------------------------------------------------------

def test_good_args():
    a = parse_args(SET_AC, '{"device": "office-aircon", "temp": 23, "power": true, "at": "23:00", "days": ["mon"]}')
    assert a["temp"] == 23
    assert parse_args(STATE, "") == {}


@pytest.mark.parametrize("raw,why", [
    ("{bad", "not valid JSON"), ("[]", "must be an object"), ("{}", "missing device"),
    ('{"device": "kitchen"}', "must be one of"), ('{"device": "office-aircon", "temp": 23.0}', "whole number"),
    ('{"device": "office-aircon", "temp": true}', "whole number"), ('{"device": "office-aircon", "temp": 40}', "at most 31"),
    ('{"device": "office-aircon", "power": "yes"}', "true or false"), ('{"device": "office-aircon", "x": 1}', "unknown field x"),
    ('{"device": "office-aircon", "at": "11pm"}', "wrong format"), ('{"device": "office-aircon", "at": "23:00x"}', "wrong format"),
    ('{"device": "office-aircon", "days": "mon"}', "must be a list"),
    ('{"device": "office-aircon", "days": ["sun"]}', "days[0] must be one of"),
])
def test_bad_args_are_refused_with_a_reason(raw, why):
    with pytest.raises(ArgError) as e:
        parse_args(SET_AC, raw)
    assert why in str(e.value)


def test_long_strings_are_refused():
    t = Tool("note", "", {"type": "object", "properties": {"text": {"type": "string"}}})
    with pytest.raises(ArgError):
        parse_args(t, json.dumps({"text": "x" * 201}))


# ---- the loop ----------------------------------------------------------------------------

async def test_plain_answer():
    srv = FakeServer([completion("I only do home things.")])
    out = await lp.run_turn(srv.client(), msgs("capital of france"), [STATE, SET_AC])
    assert (out.kind, out.text, out.rounds, out.model, out.prompt_tokens) == (
        "answer", "I only do home things.", 1, "qwen-test", 900)


async def test_read_then_write_stops_at_the_write():
    srv = FakeServer([completion(calls=[call("get_home_state", {})]),
                      completion("Bedroom is now 23!", calls=[call("set_ac", {"device": "bedroom-aircon", "temp": 23},
                                                                       "c2")])])
    m = msgs("a bit cooler")
    out = await lp.run_turn(srv.client(), m, [STATE, SET_AC])
    assert out.kind == "actions" and out.text == ""          # model text after a write is never used
    assert [(a.tool.name, a.args) for a in out.actions] == [("set_ac", {"device": "bedroom-aircon", "temp": 23})]
    assert out.tool_calls == ["get_home_state", "set_ac"] and out.rounds == 2 and len(out.exchanges) == 2
    tool_msg = srv.requests[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1" and "cool 24" in tool_msg["content"]


async def test_bad_call_goes_back_as_an_error_then_is_fixed():
    srv = FakeServer([completion(calls=[call("set_ac", {"device": "bedroom-aircon", "temp": 23},  "a"),
                                        call("set_ac", {"device": "office-aircon", "temp": "23"}, "b")]),
                      completion(calls=[call("set_ac", {"device": "bedroom-aircon", "temp": 23}, "c"),
                                        call("set_ac", {"device": "office-aircon", "temp": 23}, "d")])])
    out = await lp.run_turn(srv.client(), msgs("both to 23"), [STATE, SET_AC])
    assert out.kind == "actions" and len(out.actions) == 2       # never a partial card
    assert out.tool_calls == ["set_ac", "set_ac!", "set_ac", "set_ac"]
    results = [m for m in srv.requests[1]["messages"] if m["role"] == "tool"]
    assert "fix the other calls" in results[0]["content"] and "whole number" in results[1]["content"]


async def test_unknown_tool_and_broken_read_tool():
    async def boom(args):
        raise RuntimeError("x")

    broken = Tool("get_home_state", "", {"type": "object", "properties": {}}, run=boom)
    srv = FakeServer([completion(calls=[call("rm_rf", {}, "a"), call("get_home_state", {}, "b")]), completion("Sorry.")])
    out = await lp.run_turn(srv.client(), msgs(), [broken])
    assert out.kind == "answer" and out.tool_calls == ["?!", "get_home_state"]
    results = [m["content"] for m in srv.requests[1]["messages"] if m["role"] == "tool"]
    assert "unknown tool rm_rf" in results[0] and "error" in results[1]      # reads not run when a call was bad


async def test_too_many_rounds():
    srv = FakeServer([completion(calls=[call("get_home_state", {}, f"c{i}")]) for i in range(4)])
    out = await lp.run_turn(srv.client(), msgs(), [STATE])
    assert (out.kind, out.error, out.rounds) == ("failed", "rounds", 4)


async def test_too_many_writes():
    calls = [call("set_ac", {"device": "office-aircon", "temp": 16 + i}, f"c{i}") for i in range(11)]
    out = await lp.run_turn(FakeServer([completion(calls=calls)]).client(), msgs(), [SET_AC])
    assert (out.kind, out.error) == ("failed", "too_many")


async def test_empty_reply_busy_and_down_never_raise():
    out = await lp.run_turn(FakeServer([completion("")]).client(), msgs(), [STATE])
    assert (out.kind, out.error) == ("failed", "empty")
    out = await lp.run_turn(FakeServer([], slots=[{"id": 1, "is_processing": True}]).client(), msgs(), [STATE])
    assert (out.kind, out.error, out.rounds) == ("failed", "busy", 0)
    out = await lp.run_turn(FakeServer([httpx.Response(500)]).client(), msgs(), [STATE])
    assert (out.kind, out.error) == ("failed", "http")


async def test_whole_turn_is_capped():
    async def slow(request):
        if request.url.path == "/slots":
            return httpx.Response(200, json=[])
        await asyncio.sleep(5)

    c = LlmClient("http://llm", 1, httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    out = await lp.run_turn(c, msgs(), [STATE], timeout_s=0.2)
    assert (out.kind, out.error) == ("failed", "timeout")


# ---- review follow-ups -------------------------------------------------------------------

async def test_replay_log_has_what_each_round_really_sent_and_caller_list_is_untouched():
    srv = FakeServer([completion(calls=[call("get_home_state", {})]),
                      completion(calls=[call("set_ac", {"device": "office-aircon"}, "c2")])])
    m = msgs()
    out = await lp.run_turn(srv.client(), m, [STATE, SET_AC])
    assert [len(e["request"]["messages"]) for e in out.exchanges] == [2, 4]
    assert len(m) == 2                                         # no half rounds left for the chat context


async def test_unserialisable_read_result_is_a_tool_error():
    async def loop_ref(args):
        d = {}
        d["self"] = d
        return d

    t = Tool("get_home_state", "", {"type": "object", "properties": {}}, run=loop_ref)
    srv = FakeServer([completion(calls=[call("get_home_state", {})]), completion("ok")])
    out = await lp.run_turn(srv.client(), msgs(), [t])
    assert out.kind == "answer" and "tool failed" in srv.requests[1]["messages"][-1]["content"]


async def test_cut_off_reply_is_a_failure():
    r = completion("The bedroom AC is")
    r["choices"][0]["finish_reason"] = "length"
    out = await lp.run_turn(FakeServer([r]).client(), msgs(), [STATE])
    assert (out.kind, out.error, out.text) == ("failed", "truncated", "")


async def test_write_on_the_last_call_is_accepted():
    srv = FakeServer([completion(calls=[call("get_home_state", {}, f"c{i}")]) for i in range(3)]
                     + [completion(calls=[call("set_ac", {"device": "office-aircon"}, "w")])])
    out = await lp.run_turn(srv.client(), msgs(), [STATE, SET_AC])
    assert (out.kind, out.rounds) == ("actions", 4)


async def test_reads_with_writes_read_first_then_card_with_note():
    w = {"device": "office-aircon", "power": False}
    srv = FakeServer([completion(calls=[call("get_home_state", {}, "a"), call("set_ac", w, "b"), call("set_ac", w, "c")]),
                      completion("The bedroom is on at 24C.")])
    out = await lp.run_turn(srv.client(), msgs(), [STATE, SET_AC])
    assert out.kind == "actions" and [a.args for a in out.actions] == [w]     # duplicates merge
    assert out.text == "The bedroom is on at 24C." and out.rounds == 2
    results = [m for m in srv.requests[1]["messages"] if m.get("role") == "tool"]
    assert "bedroom-aircon" in results[0]["content"] and "ready" in results[1]["content"]


async def test_waiting_card_survives_reads_new_writes_timeouts_and_errors():
    w, w2 = {"device": "office-aircon", "power": False}, {"device": "office-aircon", "power": True}
    first = completion(calls=[call("get_home_state", {}, "a"), call("set_ac", w, "b")])
    srv = FakeServer([first, completion(calls=[call("get_home_state", {}, "c")]),
                      completion("Done, it is now off.")])                   # a claim: the note is dropped
    out = await lp.run_turn(srv.client(), msgs(), [STATE, SET_AC])
    assert out.kind == "actions" and [a.args for a in out.actions] == [w] and out.text == ""
    # A write after "ready" never swaps or drops the card's lines
    srv = FakeServer([first, completion(calls=[call("set_ac", w2, "c")]), completion("The bedroom is on.")])
    out = await lp.run_turn(srv.client(), msgs(), [STATE, SET_AC])
    assert [a.args for a in out.actions] == [w] and out.text == "The bedroom is on."
    assert "already made" in [m for m in srv.requests[2]["messages"] if m.get("role") == "tool"][-1]["content"]
    cut = completion("x")
    cut["choices"][0]["finish_reason"] = "length"
    out = await lp.run_turn(FakeServer([first, cut]).client(), msgs(), [STATE, SET_AC])
    assert out.kind == "actions" and out.text == ""
    # The answer call times out or the server errors: the card still shows, without a note
    out = await lp.run_turn(FakeServer([first]).client(), msgs(), [STATE, SET_AC])   # no 2nd reply: error
    assert out.kind == "actions" and [a.args for a in out.actions] == [w] and out.note_error


async def test_reads_in_an_earlier_round_still_get_their_answer():
    w = {"device": "office-aircon", "power": False}
    srv = FakeServer([completion(calls=[call("get_home_state", {}, "a")]), completion(calls=[call("set_ac", w, "b")]),
                      completion("The bedroom is on at 24C.")])
    out = await lp.run_turn(srv.client(), msgs(), [STATE, SET_AC])
    assert out.kind == "actions" and out.text == "The bedroom is on at 24C." and out.rounds == 3


async def test_reads_with_writes_in_the_last_round_make_the_card():
    w = {"device": "office-aircon", "power": False}
    srv = FakeServer([completion(calls=[call("get_home_state", {}, str(i))]) for i in range(lp.MAX_TOOL_ROUNDS)]
                     + [completion(calls=[call("get_home_state", {}, "x"), call("set_ac", w, "y")])])
    out = await lp.run_turn(srv.client(), msgs(), [STATE, SET_AC])
    assert out.kind == "actions" and [a.args for a in out.actions] == [w]


@pytest.mark.parametrize("text", ["Done!", "Saved.", "The office aircon is now off.", "Turned it off for you.",
                                  "Sure", "I'll turn it off now.", "<tool_call>x"])
async def test_card_note_never_claims_a_result(text):
    srv = FakeServer([completion(text, [call("set_ac", {"device": "office-aircon", "power": False}, "a")])])
    out = await lp.run_turn(srv.client(), msgs(), [STATE, SET_AC])
    assert out.kind == "actions" and out.text == ""


async def test_card_note_is_capped():
    srv = FakeServer([completion("word " * 400, [call("set_ac", {"device": "office-aircon", "power": False}, "a")])])
    out = await lp.run_turn(srv.client(), msgs(), [STATE, SET_AC])
    assert out.text.endswith("...") and len(out.text) <= lp.MAX_NOTE_CHARS + 3


async def test_unexpected_crash_is_a_failed_turn(monkeypatch):
    async def boom(*a, **k):
        raise KeyError("x")

    c = FakeServer([]).client()
    monkeypatch.setattr(c, "chat", boom)
    out = await lp.run_turn(c, msgs(), [STATE])
    assert (out.kind, out.error) == ("failed", "bug")


async def test_any_slot_skips_the_check_and_bad_url_is_down():
    await FakeServer([], slots=[{"id": 1, "is_processing": True}]).client(slot=None).check_slot()
    c = LlmClient("127.0.0.1:8001", 1)               # no scheme
    for coro in (c.check_slot(), c.chat(msgs())):
        with pytest.raises(LlmError) as e:
            await coro
        assert e.value.code == "down"
    await c.aclose()


def test_enum_matches_type_too_and_schema_typos_are_caught():
    t = Tool("x", "", {"type": "object", "properties": {"n": {"type": "integer", "enum": [1, 2]}}})
    with pytest.raises(ArgError):
        parse_args(Tool("y", "", {"type": "object", "properties": {"b": {"enum": [1]}}}), '{"b": true}')
    assert parse_args(t, '{"n": 1}') == {"n": 1}
    assert schema_problems(SET_AC.parameters) == [] and schema_problems(STATE.parameters) == []
    bad = {"type": "object", "required": ["z"], "properties": {"a": {"type": "number"},
                                                               "l": {"type": "array", "items": {"type": "str"}}}}
    assert schema_problems(bad) == ["parameters: required 'z' is not a property", "a: unsupported type 'number'",
                                    "l[]: unsupported type 'str'"]


async def test_leaked_tool_call_markup_is_retried_never_shown():
    leak = "<tool_call>=call>\n<parameter=action>\nresume_schedule\n</parameter>"
    srv = FakeServer([completion(leak), completion("Resumed? No: it was not paused.")])
    out = await lp.run_turn(srv.client(), msgs(), [STATE])
    assert (out.kind, out.text, out.tool_calls) == ("answer", "Resumed? No: it was not paused.", ["?!"])
    assert "could not be read" in srv.requests[1]["messages"][-1]["content"]
    srv = FakeServer([completion(leak)] * 5)
    out = await lp.run_turn(srv.client(), msgs(), [STATE])
    assert (out.kind, out.error, out.text) == ("failed", "bad_tool_call", "")


@pytest.mark.parametrize("text,note", [
    ("I have added the schedule. You have a 07:00 on schedule, and it is paused.",
     "You have a 07:00 on schedule, and it is paused."),
    ("The timer is added for 2 hours from now. Bed time is on, not paused.", "Bed time is on, not paused."),
    ("The new schedule is ready for confirmation.\n\nThe 07:00 one is paused.", "The 07:00 one is paused."),
    ("The bedroom aircon is set to turn off. The office is ON cool 25C.", "The office is ON cool 25C."),
    ("-", ""),
    ("The 7:00 schedule was created by Mary on Monday.", "The 7:00 schedule was created by Mary on Monday."),
    ("I checked: the bedroom is set to 24C.", "I checked: the bedroom is set to 24C."),
    ("Turning off the office aircon now. All set!", ""),
    ("Okay, the office aircon goes off at 7.", ""),
])
def test_card_note_keeps_only_sentences_that_claim_nothing(text, note):
    assert lp._note(text) == note


async def test_ask_rest_asks_once_more_when_the_card_came_without_reads():
    w = {"device": "office-aircon", "power": False}
    srv = FakeServer([completion(calls=[call("set_ac", w, "a")]), completion("Bed time is on.")])
    out = await lp.run_turn(srv.client(), msgs(), [STATE, SET_AC], ask_rest=True)
    assert out.kind == "actions" and out.text == "Bed time is on." and out.rounds == 2
    srv = FakeServer([completion(calls=[call("set_ac", w, "a")])])
    out = await lp.run_turn(srv.client(), msgs(), [STATE, SET_AC])          # no question: one call
    assert out.kind == "actions" and out.rounds == 1


async def test_already_like_that_becomes_the_left_out_note():
    from iotbot.llm.cards import Prepared
    from iotbot.llm.tools import AlreadyError

    async def prep(args):
        if args["device"] == "office-aircon":
            raise AlreadyError("office-aircon is already OFF (as last sent)")
        return Prepared(args["device"], "ON -> OFF", None, lambda: 0)

    tool = Tool("set_ac", "Set", SET_AC.parameters, write=True, prepare=prep)
    off = [call("set_ac", {"device": d, "power": False}, d) for d in ("bedroom-aircon", "office-aircon")]
    srv = FakeServer([completion(calls=off), completion(calls=[off[0]])])
    out = await lp.run_turn(srv.client(), msgs(), [tool])
    assert out.kind == "actions" and len(out.actions) == 1
    assert out.text == "Left out: office-aircon is already OFF (as last sent)."


def test_card_note_drops_state_claims_about_what_the_card_changes():
    from iotbot.llm.cards import Prepared
    acts = [lp.Action(SET_AC, {"device": "office-aircon"}, Prepared("office-aircon", "ON -> OFF", None, lambda: 0))]
    subj = lp._subjects(acts)
    assert lp._note("The office aircon is off. The bedroom is on at 24C.", subj) == "The bedroom is on at 24C."
    assert lp._note("The office aircon is set to 23C.", subj) == "The office aircon is set to 23C."
