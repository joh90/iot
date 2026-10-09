import asyncio
import json
from pathlib import Path

import pytest

from iotbot.ac import mitsubishi as m
from iotbot.ac.state import AcState
from iotbot.bot import handlers as hd
from iotbot.config import ConfigError, load_settings
from iotbot.context import build_context
from iotbot.devices.hub import HubError
from iotbot.result import Actor
from tests.test_bot import ME, NoNetTransport, make_context, make_update, replied

REAL = json.loads((Path(__file__).parent.parent / "commands.json").read_text())["1"]["mitsubishi"]
DAIKIN_ON = "2600" + "0a" * 8
ALICE = Actor(ME, "Alice", "button")
BEDROOM = AcState(power=True, temp=22, fan=3, vane="auto")
OFFICE = AcState(power=True, temp=23, fan=3, vane=1)


def frames_of(packet):
    return m.pulses_to_frames(m.packet_to_pulses(packet))


def make_ctx(tmp_path, env=None, commands=None):
    (tmp_path / "devices.json").write_text(json.dumps({
        "bedroom": {"mac_address": "780f771abcde", "broadlink_type": "RMMINI",
                    "devices": [{"type": 1, "id": "bed_ac", "brand": "mitsubishi", "model": "fn18ve-22"}]},
        "office": {"mac_address": "780f771abcdf", "broadlink_type": "RMMINI",
                   "devices": [{"type": 1, "id": "office_ac", "brand": "mitsubishi", "model": "fn18ve-23"},
                               {"type": 1, "id": "old_ac", "brand": "daikin", "model": "nx"}]},
    }))
    (tmp_path / "commands.json").write_text(json.dumps(commands or {"1": {
        "mitsubishi": REAL, "daikin": {"nx": {"power_on": DAIKIN_ON, "power_off": DAIKIN_ON}}}}))
    (tmp_path / "users.json").write_text(json.dumps({str(ME): "Alice"}))
    settings = load_settings(environ={"BOT_TOKEN": "1:x", **(env or {})}, base_dir=tmp_path)
    ctx = build_context(settings, transport=NoNetTransport())
    ctx.sent = []

    async def send_ir(mac, packets, gap_s=0.0, idempotent=False):
        if getattr(ctx, "fail", None):
            raise ctx.fail
        ctx.sent.append((mac, packets, idempotent))

    ctx.hub.send_ir = send_ir
    return ctx


def events(tmp_path):
    return [json.loads(line) for f in sorted((tmp_path / "logs").glob("device_events*"))
            for line in f.read_text().splitlines()]


def test_presets_come_from_captured_power_on(tmp_path):
    ctx = make_ctx(tmp_path)
    assert ctx.ac.presets == {"bed_ac": BEDROOM, "office_ac": OFFICE}
    assert not ctx.ac.manages("old_ac")
    assert not ctx.ac.manages("bed_ac", "toggle_swing")


@pytest.mark.parametrize("dev,model,feature", [
    (d, mdl, f) for d, mdl in (("bed_ac", "fn18ve-22"), ("office_ac", "fn18ve-23"))
    for f in ("power_on", "powerful", "power_off")])
async def test_buttons_send_encoded_frames_equal_to_captures(tmp_path, dev, model, feature):
    ctx = make_ctx(tmp_path)
    r = await ctx.devices.run(dev, feature, ALICE)
    assert r.ok, r.message
    (_, packets, idempotent), = ctx.sent
    assert idempotent
    assert packets[0] != bytes.fromhex(REAL[model][feature])        # generated, not the capture
    assert frames_of(packets[0]) == frames_of(bytes.fromhex(REAL[model][feature]))


async def test_on_logs_saves_and_describes(tmp_path):
    ctx = make_ctx(tmp_path)
    r = await ctx.devices.run("bed_ac", "power_on", ALICE)
    assert r.message == "Sent bed_ac power on: ON cool 22C fan 3 vane auto"
    assert events(tmp_path)[-1]["ac_state"] == BEDROOM.to_dict()
    sent = ctx.ac.last("bed_ac")
    assert (sent.state, sent.actor) == (BEDROOM, "Alice")
    assert json.loads((tmp_path / "state" / "ac_state.json").read_text())["bed_ac"]["state"]["temp"] == 22


async def test_off_uses_last_state_and_clears_powerful(tmp_path):
    ctx = make_ctx(tmp_path)
    await ctx.devices.run("office_ac", "powerful", ALICE)
    assert ctx.ac.last("office_ac").state == OFFICE.with_changes(fan="auto", powerful=True)
    await ctx.devices.run("office_ac", "power_off", ALICE)
    assert ctx.ac.last("office_ac").state == OFFICE.with_changes(fan="auto", power=False)
    await ctx.devices.run("office_ac", "power_on", ALICE)
    assert ctx.ac.last("office_ac").state == OFFICE


async def test_failed_send_not_saved_but_logged(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.fail = HubError("RM offline")
    r = await ctx.devices.run("bed_ac", "power_on", ALICE)
    assert not r.ok
    assert ctx.ac.last("bed_ac") is None
    ev = events(tmp_path)[-1]
    assert ev["ok"] is False and ev["ac_state"] == BEDROOM.to_dict()


async def test_save_failure_is_a_warning(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)

    async def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(ctx.ac.store, "put", boom)
    r = await ctx.devices.run("bed_ac", "power_on", ALICE)
    assert r.ok and r.warnings and "could not save" in r.warnings[0]


async def test_daikin_still_sends_captured_codes(tmp_path):
    ctx = make_ctx(tmp_path)
    await ctx.devices.run("old_ac", "power_on", ALICE)
    assert ctx.sent[0][1] == (bytes.fromhex(DAIKIN_ON),)
    assert "ac_state" not in events(tmp_path)[-1]


async def test_kill_switch_sends_captures(tmp_path):
    ctx = make_ctx(tmp_path, env={"AC_ENCODER": "off"})
    assert ctx.ac is None and ctx.devices.ac is None
    await ctx.devices.run("bed_ac", "power_on", ALICE)
    assert ctx.sent[0][1] == (bytes.fromhex(REAL["fn18ve-22"]["power_on"]),)
    assert not (tmp_path / "state" / "ac_state.json").exists()


@pytest.mark.parametrize("value,expected", [("on", True), ("OFF", False), (" off ", False), ("", True)])
def test_ac_encoder_setting(value, expected):
    s = load_settings(environ={"BOT_TOKEN": "1:x", "AC_ENCODER": value})
    assert s.ac_encoder is expected


def test_ac_encoder_setting_rejects_typos():
    with pytest.raises(ConfigError, match="AC_ENCODER"):
        load_settings(environ={"BOT_TOKEN": "1:x", "AC_ENCODER": "no"})


async def test_unusable_capture_falls_back_with_warning(tmp_path):
    odd = {**REAL, "fn18ve-22": {**REAL["fn18ve-22"], "power_on": REAL["fn18ve-22"]["power_off"]},
           "fn18ve-23": {**REAL["fn18ve-23"], "power_on": DAIKIN_ON}}
    ctx = make_ctx(tmp_path, commands={"1": {"mitsubishi": odd}})
    assert ctx.ac.presets == {}
    assert any("bed_ac" in w and "not a plain On" in w for w in ctx.warnings)
    assert any("office_ac" in w and "sending captured codes" in w for w in ctx.warnings)
    await ctx.devices.run("office_ac", "power_on", ALICE)
    assert ctx.sent[0][1] == (bytes.fromhex(DAIKIN_ON),)


async def test_send_ac_state(tmp_path):
    ctx = make_ctx(tmp_path)
    want = AcState(power=True, temp=26, fan="quiet", vane="swing")
    r = await ctx.devices.send_ac_state("bed_ac", want, ALICE, source="sched-1")
    assert r.ok and r.data["feature"] == "set_state"
    assert frames_of(ctx.sent[0][1][0]) == [m.encode(want)] * 2
    assert ctx.ac.last("bed_ac").state == want
    assert events(tmp_path)[-1]["source"] == "sched-1"
    r = await ctx.devices.send_ac_state("old_ac", want, ALICE)
    assert not r.ok and r.error == "not_supported"
    r = await ctx.devices.send_ac_state("nope", want, ALICE)
    assert r.error == "device_not_found"


async def test_concurrent_sends_save_in_send_order(tmp_path):
    ctx = make_ctx(tmp_path)
    gate = asyncio.Event()
    order = []

    async def slow_send(mac, packets, gap_s=0.0, idempotent=False):
        order.append(m.decode(frames_of(packets[0])[0]).power)
        if len(order) == 1:
            await gate.wait()

    ctx.hub.send_ir = slow_send
    first = asyncio.create_task(ctx.devices.run("bed_ac", "power_on", ALICE))
    await asyncio.sleep(0)
    second = asyncio.create_task(ctx.devices.run("bed_ac", "power_off", ALICE))
    await asyncio.sleep(0.01)
    assert order == [True]          # second waits for the first to finish and save
    gate.set()
    await asyncio.gather(first, second)
    assert order == [True, False]
    assert ctx.ac.last("bed_ac").state.power is False


async def test_status_shows_aircons(tmp_path):
    ctx = make_ctx(tmp_path)
    await ctx.devices.run("bed_ac", "power_on", ALICE)
    u = make_update(ME)
    await hd.cmd_status(u, make_context(ctx))
    text = replied(u)
    assert "Aircons (last state sent by the bot)" in text
    assert "bed_ac: ON cool 22C fan 3 vane auto (" in text and "Alice)" in text
    assert "office_ac: nothing sent yet (On = ON cool 23C fan 3 vane 1)" in text
    assert "old_ac:" not in text


async def test_status_shows_kill_switch(tmp_path):
    ctx = make_ctx(tmp_path, env={"AC_ENCODER": "off"})
    u = make_update(ME)
    await hd.cmd_status(u, make_context(ctx))
    assert "AC encoder: off" in replied(u)


async def test_cancel_after_send_still_saves_and_logs(tmp_path):
    ctx = make_ctx(tmp_path)
    gate = asyncio.Event()
    put = ctx.ac.store.put

    async def slow_put(*a, **kw):
        await gate.wait()
        await put(*a, **kw)

    ctx.ac.store.put = slow_put
    task = asyncio.create_task(ctx.devices.run("bed_ac", "power_on", ALICE))
    await asyncio.sleep(0.01)
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.01)
    assert ctx.ac.last("bed_ac").state == BEDROOM
    assert events(tmp_path)[-1]["ok"] is True


async def test_encoder_failure_is_not_maybe_delivered(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)

    def broken(state):
        raise ValueError("bad")

    monkeypatch.setattr(ctx.ac, "packet", broken)
    r = await ctx.devices.run("bed_ac", "power_on", ALICE)
    assert not r.ok and r.data["maybe_delivered"] is False
    assert not ctx.sent and ctx.ac.last("bed_ac") is None


async def test_button_popup_shows_save_warning(tmp_path, monkeypatch):
    from iotbot.bot import keyboards as kb
    from iotbot.bot.callbacks import encode
    ctx = make_ctx(tmp_path)

    async def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(ctx.ac.store, "put", boom)
    u = make_update(ME, data=encode(kb.KB, "f", "bed_ac", "power_on"))
    await hd.on_button(u, make_context(ctx))
    (text,), kw = u.callback_query.answer.call_args
    assert "could not save" in text and kw["show_alert"] is True
