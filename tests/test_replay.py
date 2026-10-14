"""Phase 4.5: the LLM replay log, chat stats and chat turn fields (fake LLM, no IR)."""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from iotbot.bot import handlers as hd
from iotbot.bot import stats
from iotbot.llm.replay import ReplayLog
from iotbot.store import JsonlLog
from iotbot.telemetry import Tracker
from tests.test_ac_service import make_ctx
from tests.test_bot import ME, make_update
from tests.test_chat import Clock, say, tg_context
from tests.test_llm import FakeServer, call, completion

SGT = ZoneInfo("Asia/Singapore")


def ex(system="S", tools=("t",), user="hi"):
    return {"request": {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                        "tools": list(tools)}, "response": {"x": 1}, "ms": 5}


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


async def test_prompts_are_written_once_per_day(tmp_path):
    clock = Clock(datetime(2026, 10, 14, 23, 59, tzinfo=SGT).timestamp())
    log = ReplayLog(tmp_path, "Asia/Singapore", clock=clock)
    original = ex()
    await log.write(lambda: {"text": "a"}, [original, ex(user="tool round")])
    await log.write(lambda: {"text": "b"}, [ex(system="S2")])
    got = lines(tmp_path / "llm-2026-10-14.jsonl")
    assert [x["kind"] for x in got] == ["prompt", "turn", "prompt", "turn"]
    assert got[0]["system"] == "S" and got[0]["tools"] == ["t"]
    req = got[1]["exchanges"][0]["request"]
    assert req["messages"][0]["content"] == f"@prompt:{got[0]['hash']}" and req["tools"] == req["messages"][0]["content"]
    assert got[1]["exchanges"][1]["request"]["messages"][1]["content"] == "tool round"
    assert original["request"]["messages"][0]["content"] == "S"            # the caller's data is untouched
    clock.t += 120                                                         # a new day: a new file, prompt again
    await log.write(lambda: {"text": "c"}, [ex()])
    assert [x["kind"] for x in lines(tmp_path / "llm-2026-10-15.jsonl")] == ["prompt", "turn"]


async def test_old_days_are_deleted_and_nothing_else(tmp_path):
    for name in ("llm-2026-07-15.jsonl", "llm-2026-07-16.jsonl", "notes.txt", "llm-bad.jsonl"):
        (tmp_path / name).write_text("x\n")
    clock = Clock(datetime(2026, 10, 14, 12, tzinfo=SGT).timestamp())
    await ReplayLog(tmp_path, "Asia/Singapore", clock=clock).write(dict, [ex()])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["llm-2026-07-16.jsonl", "llm-2026-10-14.jsonl",
                                                          "llm-bad.jsonl", "notes.txt"]


async def test_a_broken_log_never_raises(tmp_path):
    (tmp_path / "file").write_text("x")
    await ReplayLog(tmp_path / "file" / "llm", "Asia/Singapore").write(dict, [ex()])


# ---- chat turns end to end ---------------------------------------------------------------

async def test_chat_turn_fields_replay_and_card_tap_join(tmp_path):
    ctx = make_ctx(tmp_path)
    from iotbot.ac.state import AcState
    from iotbot.result import Actor
    await ctx.devices.send_ac_state("bed_ac", AcState(power=True, temp=24), Actor(ME, "Alice", "button"))
    ctx.assistant.llm = FakeServer([completion(calls=[call("set_ac", {"device": "bed_ac", "power": False})])]).client()
    context = tg_context(ctx)
    tracker = Tracker(JsonlLog(tmp_path / "t", "turns"))
    u = say("off the bedroom ac")
    u.effective_message.reply_text = AsyncMock(return_value=SimpleNamespace(chat_id=ME, message_id=9))
    async with tracker.turn(surface="text", request_id="tg:1") as t:
        await hd.on_text(u, context)
    assert (t.intent, t.outcome, t.card, t.model, t.tool_calls, t.llm_rounds) == (
        "llm:actions", "ok", "shown", "qwen-test", ["set_ac"], 1)
    async with tracker.turn(surface="button") as tap:
        await hd.on_button(make_update(ME, data=f"lc:do:{t.card_id}"), context)
    assert (tap.card, tap.card_id, tap.outcome) == ("do", t.card_id, "ok")
    await ctx.assistant.drain()
    rec = next(x for x in lines(next((tmp_path / "logs" / "llm").glob("llm-*.jsonl"))) if x["kind"] == "turn")
    assert rec["request_id"] == "tg:1" and rec["result"] == "actions" and rec["text"] == "off the bedroom ac"
    assert rec["actions"][0]["lines"][0].endswith("-> OFF (cool 24C fan auto vane auto)")


# ---- /stats --------------------------------------------------------------------------------

def turn(intent, outcome="ok", **kw):
    return {"surface": "text", "intent": intent, "outcome": outcome, "allowed": True, "total_ms": 500,
            "llm_ms": 300, "tokens": 900, **kw}


def test_chat_section(tmp_path):
    ts = ([turn("llm:answer")] * 3 + [turn("llm:actions", card="shown", card_id=c) for c in ("c1", "c2", "c3")]
          + [turn("llm:failed", "down"), turn("llm:failed", "truncated"), turn("text", "llm_off")]
          + [turn("llm:limited", "limited"), turn("llm:busy_you", "busy_you")]
          + [{"surface": "button", "intent": "lc:do", "card": "do", "card_id": "c1", "outcome": "ok"},
             {"surface": "button", "intent": "lc:x", "card": "x", "card_id": "c2", "outcome": "cancelled"},
             {"surface": "button", "intent": "lc:undo", "card": "undo", "card_id": "c1", "outcome": "ok"},
             {"surface": "button", "intent": "lc:undo", "card": "undo", "card_id": "c1", "outcome": "ok"},
             {"surface": "button", "intent": "lc:do", "card": "do", "card_id": "c3", "outcome": "refused"},
             {"surface": "button", "intent": "lc:do", "card": "do", "card_id": "old", "outcome": "ok"}])
    out = "\n".join(stats.chat_lines(ts))
    assert "- messages: 11 (3 answers, 3 cards, 3 failed: " in out
    assert "1 PC off or unreachable" in out and "1 other" in out and "1 switched off" in out
    assert "2 turned away (busy, limit or too long)" in out
    assert "- reply time: 500 / 500 ms (6x)" in out and "- of which the PC: 300 / 300 ms (6x)" in out
    assert "- tokens per message: 900 / 900 tokens (6x)" in out
    # the card from before the window and the refused tap do not count; Undo again is one card
    assert "- cards: 3 shown, 1 done, 1 cancelled, 1 not tapped; 1 undone, 0 retried" in out
    assert stats.chat_lines([]) == ["- no chat messages yet"]
    full = "\n".join(stats.summary(ts, [], make_ctx(tmp_path).registry, 7, str))
    assert "<b>Chat assistant</b>\n- messages: 11" in full


async def test_failed_write_repeats_its_prompt_and_restart_is_fine(tmp_path, monkeypatch):
    import builtins

    log = ReplayLog(tmp_path, "Asia/Singapore")
    real_open = builtins.open

    def broken(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(builtins, "open", broken)
    await log.write(lambda: {"text": "a"}, [ex()])                  # logged, never raised
    monkeypatch.setattr(builtins, "open", real_open)
    await log.write(lambda: {"text": "b"}, [ex()])
    await ReplayLog(tmp_path, "Asia/Singapore").write(lambda: {"text": "c"}, [ex()])     # after a restart
    got = lines(next(tmp_path.glob("llm-*.jsonl")))
    assert [x["kind"] for x in got] == ["prompt", "turn", "prompt", "turn"]


async def test_no_replay_line_without_a_model_call(tmp_path):
    from tests.test_chat import ALICE, make_assistant

    a, _ = make_assistant([])
    a.replay = ReplayLog(tmp_path, "Asia/Singapore")
    a.llm = FakeServer([], slots=[{"id": 1, "is_processing": True}]).client()
    assert (await a.handle(ALICE, "hi")).turn.error == "busy"
    await a.drain()
    assert list(tmp_path.iterdir()) == []
