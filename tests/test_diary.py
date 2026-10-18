"""Phase 4.7: the diary tool (logs only: no LLM, no IR)."""

from datetime import datetime, timedelta

import pytest

from iotbot.ac.state import AcState
from iotbot.devices.hub import HubError
from iotbot.llm import memory as mm
from iotbot.llm.diary import MAX_LINES, DiaryTools
from iotbot.result import Actor
from tests.test_ac_service import make_ctx
from tests.test_bot import ME

ALICE = Actor(ME, "Alice", "llm")
BOB = Actor(222, "Bob", "llm")


@pytest.fixture
def ctx(tmp_path):
    return make_ctx(tmp_path)


async def search(ctx, actor=ALICE, **args):
    return await DiaryTools(ctx, ctx.memory).tools(actor)[0].run(args)


def say(ctx, user_id, text, when=None):
    ctx.tracker.log.append({"surface": "text", "user_id": user_id, "raw": text, "intent": "llm:answer"}, when=when)


async def test_device_and_schedule_lines_say_who_and_how(ctx):
    await ctx.devices.send_ac_state("bed_ac", AcState(power=True, temp=23, fan=3), Actor(ME, "Alice", "button"))
    ctx.fail = HubError("no reply")
    await ctx.devices.run("office_ac", "power_off", Actor(0, "schedule x", "scheduler"))
    ctx.fail = None
    r = ctx.schedules.plan(Actor(ME, "Alice", "button"), {
        "device": "bed_ac", "action": {"kind": "off"}, "label": "Wake",
        "when": {"kind": "weekly", "time": "07:00", "days": ["mon"]}})
    await ctx.schedules.apply(Actor(ME, "Alice", "button"), r.data["plan_id"])
    lines = (await search(ctx))["lines"]
    tails = {x[17:] for x in lines}                    # without "Wed 14 Oct 20:42 " (same-second order varies)
    assert tails == {"Alice created schedule 'Wake' (bed_ac)",
                     "office_ac: set OFF (cool 23C fan 3 vane 1), by a schedule via schedule: FAILED",
                     "bed_ac: set ON cool 23C fan 3 vane auto, by Alice via buttons: ok"}
    office = (await search(ctx, device="office_ac"))["lines"]
    assert len(office) == 1 and "office_ac" in office[0]
    assert [x[17:] for x in (await search(ctx, words="WAKE"))["lines"]] == ["Alice created schedule 'Wake' (bed_ac)"]
    assert len((await search(ctx, what="devices"))["lines"]) == 2


async def test_only_my_own_messages(ctx):
    say(ctx, ME, "a bit cooler please")
    say(ctx, 222, "Bob's secret question")
    ctx.tracker.log.append({"surface": "button", "user_id": ME, "raw": "kb:f"})
    mine = (await search(ctx, what="my_messages"))["lines"]
    assert len(mine) == 1 and mine[0].endswith('you said: "a bit cooler please"')
    bobs = (await search(ctx, BOB, what="my_messages"))["lines"]
    assert len(bobs) == 1 and "secret" in bobs[0]
    assert (await search(ctx, what="my_messages", device="bed_ac"))["lines"] == "nothing found"


async def test_old_lines_and_the_cap(ctx):
    old = datetime.now(ctx.devices.events.tz) - timedelta(days=10)
    say(ctx, ME, "ancient", when=old)
    for i in range(MAX_LINES + 5):
        say(ctx, ME, f"msg {i}")
    r = await search(ctx, what="my_messages")
    assert len(r["lines"]) == MAX_LINES and r["more"].startswith("5 older lines")
    assert all("ancient" not in x for x in r["lines"])
    assert any("ancient" in x for x in (await search(ctx, what="my_messages", days=30, words="ancient"))["lines"])


async def test_forgotten_notes_hide_old_messages(ctx):
    say(ctx, ME, "remember that I sleep at 23 with fan quiet")
    say(ctx, ME, "make the office 24")
    nid, _ = await ctx.memory.add_fact(ALICE, "I sleep at 23 with fan quiet", False)
    await ctx.memory.remove(ME, nid)
    lines = (await search(ctx, what="my_messages"))["lines"]
    assert len(lines) == 1 and "office 24" in lines[0]
    assert "sleep" not in str(ctx.memory.store.data)                    # only hashes are kept
    item = {"id": nid, "text": "I sleep at 23 with fan quiet", "at": "x"}
    assert await ctx.memory.restore(ME, item)                          # Undo: shown again
    assert len((await search(ctx, what="my_messages"))["lines"]) == 2


def fp(text):
    f = mm.fingerprint(text)
    return [{"pairs": set(f["pairs"]), "words": set(f["words"])}]


def test_forgotten_long_and_short_notes():
    long = fp("I sleep at 23 with fan quiet")
    assert mm.mentions_forgotten("note: i sleep at 23 with fan quiet!", long)
    assert not mm.mentions_forgotten("set the fan to quiet", long)
    short = fp("door code 4821")
    assert mm.mentions_forgotten("the door code is 4821", short)
    assert not mm.mentions_forgotten("what is the door code", short)
    assert mm.mentions_forgotten("bedroom is cold", fp("bedroom"))
    assert not mm.mentions_forgotten("turn it on please", fp("it is on"))      # stop words only: never hides


async def test_saving_a_forgotten_note_again_unhides_it(ctx):
    say(ctx, ME, "remember door code 4821")
    nid, _ = await ctx.memory.add_fact(ALICE, "door code 4821", False)
    await ctx.memory.remove(ME, nid)
    assert (await search(ctx, what="my_messages"))["lines"] == "nothing found"
    await ctx.memory.add_fact(ALICE, "door code 4821", False)
    assert len((await search(ctx, what="my_messages"))["lines"]) == 1


async def test_whole_words_only_and_schedule_device_filter(ctx):
    await ctx.devices.send_ac_state("bed_ac", AcState(power=True, temp=23), Actor(ME, "Alice", "button"))
    assert (await search(ctx, words="on"))["lines"] != "nothing found"            # the ON in the state
    assert (await search(ctx, words="butt"))["lines"] == "nothing found"          # not part of "buttons"
    r = ctx.schedules.plan(Actor(ME, "Alice", "button"), {
        "device": "office_ac", "action": {"kind": "off"}, "when": {"kind": "weekly", "time": "07:00", "days": ["mon"]}})
    await ctx.schedules.apply(Actor(ME, "Alice", "button"), r.data["plan_id"])
    assert len((await search(ctx, what="schedules", device="office_ac"))["lines"]) == 1
    assert (await search(ctx, what="schedules", device="bed_ac"))["lines"] == "nothing found"


async def test_odd_lines_and_former_users(ctx):
    ctx.devices.events.append({"device": "bed_ac", "feature": "power_on", "user_id": 99999, "surface": "button",
                               "ok": True, "ac_state": {"power": "maybe"}})
    line = (await search(ctx, what="devices"))["lines"][0]
    assert line.endswith("bed_ac: power on, by a former user via buttons: ok") and "99999" not in line
    ctx.schedules, ctx.tracker = None, None
    assert len((await search(ctx))["lines"]) == 1


async def test_same_text_from_someone_else_is_not_mine(ctx):
    say(ctx, 222, "make the office 24")
    assert (await search(ctx, what="my_messages"))["lines"] == "nothing found"
    assert len((await search(ctx, BOB, what="my_messages"))["lines"]) == 1


async def test_a_chat_turn_sorts_before_the_send_it_caused(ctx):
    from iotbot.telemetry import Tracker

    tracker = Tracker(ctx.tracker.log)
    async with tracker.turn(surface="text", user_id=ME, raw="off the bedroom"):
        await ctx.devices.send_ac_state("bed_ac", AcState(power=False, temp=23), ALICE)
    lines = (await search(ctx))["lines"]
    assert "bed_ac: set OFF" in lines[0] and lines[1].endswith('you said: "off the bedroom"')
