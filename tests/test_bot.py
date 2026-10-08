import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from iotbot.bot import handlers as hd
from iotbot.bot import keyboards as kb
from iotbot.bot.callbacks import CallbackTooLong, decode, encode
from iotbot.bot.text import h, human_duration, popup
from iotbot.config import load_settings
from iotbot.context import build_context, expected_devices
from iotbot.devices.hub import Transport

ON = "2600" + "0a" * 8
ME, OTHER, STRANGER = 111, 222, 999


class NoNetTransport(Transport):
    def __init__(self):
        super().__init__(1)

    def scan(self, want, timeout=None):
        return {}


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / "devices.json").write_text(json.dumps({
        "bedroom": {"mac_address": "780f771abcde", "broadlink_type": "RMMINI",
                    "devices": [{"type": 1, "id": "bedroom_ac", "brand": "daikin", "model": "nx"}],
                    "broadlink_devices": [{"id": "lamp", "mac_address": "780f77116def", "broadlink_type": "SP2"}]},
    }))
    (tmp_path / "commands.json").write_text(json.dumps(
        {"1": {"daikin": {"nx": {"power_on": ON, "power_off": ON, "toggle_swing": ON}}}}))
    (tmp_path / "users.json").write_text(json.dumps({str(ME): "Me_user", str(OTHER): "Other <b>"}))
    settings = load_settings(environ={"BOT_TOKEN": "1:x"}, base_dir=tmp_path)
    c = build_context(settings, transport=NoNetTransport())
    sent = []

    async def send_ir(mac, packets, gap_s=0.0, idempotent=False):
        sent.append((mac, packets))

    c.hub.send_ir = send_ir
    c.sent = sent
    return c


def make_update(user_id, text_args=None, data=None, message=True):
    msg = SimpleNamespace(reply_text=AsyncMock())
    query = None
    if data is not None:
        query = SimpleNamespace(
            data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
            message=SimpleNamespace() if message else None,
            from_user=SimpleNamespace(id=user_id), get_bot=lambda: SimpleNamespace(send_message=AsyncMock()),
        )
    update = SimpleNamespace(
        update_id=7, effective_user=SimpleNamespace(id=user_id, full_name="Test User"),
        effective_message=msg, callback_query=query,
    )
    return update


def make_context(ctx, args=None):
    return SimpleNamespace(args=args or [], application=SimpleNamespace(bot_data={"ctx": ctx}), error=None)


def replied(update):
    return " ".join(c.args[0] for c in update.effective_message.reply_text.call_args_list)


# ---- pure helpers -------------------------------------------------------------

def test_callback_roundtrip_and_limit():
    assert decode(encode("kb", "f", "bedroom_ac", "power_on")) == ["kb", "f", "bedroom_ac", "power_on"]
    with pytest.raises(CallbackTooLong):
        encode("kb", "f", "x" * 40, "y" * 30)


def test_worst_case_ids_fit_callback_budget():
    encode(kb.KB, "f", "x" * 32, "y" * 24)  # max id + max feature name


def test_escaping_and_popup():
    assert h("a_b <x> & *y*") == "a_b &lt;x&gt; &amp; *y*"
    assert len(popup("x" * 500)) == 200
    assert human_duration(90061) == "1d 1h 1m"


def test_device_keyboard_only_captured_features(ctx):
    markup = kb.device_keyboard(ctx.registry, "bedroom_ac")
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert labels[:3] == ["power on", "power off", "toggle swing"]


def test_users_keyboard_no_remove_for_self():
    markup = kb.users_keyboard([(ME, "Me"), (OTHER, "Other")], ME)
    rows = markup.inline_keyboard
    assert len(rows[0]) == 1 and len(rows[1]) == 2


def test_expected_devices(ctx):
    macs = {e.mac for e in expected_devices(ctx.registry)}
    assert macs == {"780f771abcde", "780f77116def"}


# ---- handlers -----------------------------------------------------------------

async def test_ping_open_to_strangers(ctx):
    u = make_update(STRANGER)
    await hd.cmd_ping(u, make_context(ctx))
    assert str(STRANGER) in replied(u)


async def test_stranger_rejected(ctx):
    u = make_update(STRANGER)
    await hd.cmd_on(u, make_context(ctx, ["bedroom_ac"]))
    assert "not approved" in replied(u)
    assert ctx.sent == []


async def test_stranger_button_rejected(ctx):
    u = make_update(STRANGER, data="kb:f:bedroom_ac:power_on")
    await hd.on_button(u, make_context(ctx))
    u.callback_query.answer.assert_awaited()
    assert ctx.sent == []


async def test_on_sends_and_replies(ctx):
    u = make_update(ME)
    await hd.cmd_on(u, make_context(ctx, ["bedroom_ac"]))
    assert ctx.sent and "Sent bedroom_ac power on" in replied(u)


async def test_d_rejects_attribute_names_b2(ctx):
    for words in (["__delattr__"], ["fire_action"], ["features"]):
        u = make_update(ME)
        await hd.cmd_device(u, make_context(ctx, ["bedroom_ac", *words]))
        assert "has no" in replied(u)
    assert ctx.sent == []


async def test_d_unknown_device_escaped_b11(ctx):
    u = make_update(ME)
    await hd.cmd_device(u, make_context(ctx, ["<b>_x", "on"]))
    assert "&lt;b&gt;_x" in replied(u)


async def test_feature_button_answers_popup(ctx):
    u = make_update(ME, data="kb:f:bedroom_ac:toggle_swing")
    await hd.on_button(u, make_context(ctx))
    args, kwargs = u.callback_query.answer.call_args
    assert args[0] == "Sent bedroom_ac toggle swing" and kwargs["show_alert"] is False
    assert "*" not in args[0]


async def test_stale_button_answered(ctx):
    for data in ("kb:d:gone", "old_handler something", "us:zzz", "kb:f:bedroom_ac:nope"):
        u = make_update(ME, data=data)
        await hd.on_button(u, make_context(ctx))
        u.callback_query.answer.assert_awaited()


async def test_not_modified_is_ignored_b28(ctx):
    u = make_update(ME, data="kb:rooms")
    u.callback_query.edit_message_text = AsyncMock(side_effect=BadRequest("Message is not modified"))
    await hd.on_button(u, make_context(ctx))  # must not raise


async def test_inaccessible_message_b27(ctx):
    u = make_update(ME, data="kb:rooms", message=False)
    await hd.on_button(u, make_context(ctx))
    u.callback_query.answer.assert_awaited()


async def test_remove_user_flow_and_double_tap_b13(ctx):
    c = make_context(ctx)
    u = make_update(ME, data=f"us:ask:{OTHER}")
    await hd.on_button(u, c)
    assert "Remove" in u.callback_query.edit_message_text.call_args.args[0]
    assert "&lt;b&gt;" in u.callback_query.edit_message_text.call_args.args[0]
    u = make_update(ME, data=f"us:del:{OTHER}")
    await hd.on_button(u, c)
    assert not ctx.users.is_allowed(OTHER)
    u = make_update(ME, data=f"us:del:{OTHER}")
    await hd.on_button(u, c)  # double tap: no crash
    assert "already removed" in u.callback_query.answer.call_args.args[0]


async def test_cannot_delete_self_even_via_crafted_button(ctx):
    u = make_update(ME, data=f"us:del:{ME}")
    await hd.on_button(u, make_context(ctx))
    assert ctx.users.is_allowed(ME)


async def test_adduser(ctx):
    u = make_update(ME)
    await hd.cmd_adduser(u, make_context(ctx, ["333", "New", "Person"]))
    assert ctx.users.is_allowed(333) and ctx.users.name_of(333) == "New Person"
    u = make_update(ME)
    await hd.cmd_adduser(u, make_context(ctx, ["333"]))
    assert "Usage" in replied(u)


async def test_status_and_list_render(ctx):
    await ctx.hub.start(expected_devices(ctx.registry))
    u = make_update(ME)
    await hd.cmd_status(u, make_context(ctx))
    text = replied(u)
    assert "OFFLINE" in text and "Me_user" in text and "Other &lt;b&gt;" in text
    u = make_update(ME)
    await hd.cmd_list(u, make_context(ctx))
    assert "bedroom_ac" in replied(u)


async def test_error_handler_answers_spinner_and_hides_update_b15(ctx, caplog):
    u = make_update(ME, data="kb:rooms")
    c = make_context(ctx)
    c.error = RuntimeError("boom")
    await hd.on_error(u, c)
    u.callback_query.answer.assert_awaited()
    assert "kb:rooms" not in caplog.text
