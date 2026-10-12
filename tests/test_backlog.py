"""Updates queued while the bot was offline, and the token log filter (task 2.12)."""

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.error import NetworkError
from telegram.ext import ApplicationHandlerStop, TypeHandler

from iotbot.bot import backlog as bl
from iotbot.main import RedactFilter

NOW = datetime(2026, 10, 12, 22, 0, tzinfo=UTC).timestamp()
SECRET = "AAG" + "s" * 32


def msg_update(uid, age_s, user_id=1):
    msg = SimpleNamespace(date=datetime.fromtimestamp(NOW - age_s, UTC), reply_text=AsyncMock())
    return SimpleNamespace(update_id=uid, message=msg, callback_query=None,
                           effective_user=SimpleNamespace(id=user_id))


def tap_update(uid, user_id=1):
    return SimpleNamespace(update_id=uid, message=None, callback_query=SimpleNamespace(answer=AsyncMock()),
                           effective_user=SimpleNamespace(id=user_id))


def backlog(cutoff=None):
    b = bl.Backlog(clock=lambda: NOW)
    b.cutoff = cutoff
    return b


def test_old_message_is_stale_recent_is_not():
    b = backlog()
    assert b.stale(msg_update(1, 121)) == "message"
    assert b.stale(msg_update(1, 30)) is None


def test_tap_queued_at_startup_is_stale():
    b = backlog(cutoff=10)
    assert b.stale(tap_update(10)) == "button"
    assert b.stale(tap_update(11)) is None
    assert backlog().stale(tap_update(1)) is None          # queue unknown: taps run


async def test_load_notes_newest_queued_id():
    b = backlog()
    bot = SimpleNamespace(get_updates=AsyncMock(return_value=[SimpleNamespace(update_id=5),
                                                              SimpleNamespace(update_id=9)]))
    await b.load(bot)
    assert b.cutoff == 9
    bot.get_updates.assert_awaited_once_with(timeout=0)      # no offset: nothing confirmed


async def test_load_failure_leaves_taps_running():
    b = backlog()
    await b.load(SimpleNamespace(get_updates=AsyncMock(side_effect=NetworkError("down"))))
    assert b.cutoff is None


def gate_context(b, allowed=(1,)):
    users = SimpleNamespace(is_allowed=lambda uid: uid in allowed)
    return SimpleNamespace(application=SimpleNamespace(bot_data={"backlog": b, "ctx": SimpleNamespace(users=users)}),
                           bot=SimpleNamespace(send_message=AsyncMock()))


def test_queued_message_with_clock_behind_is_stale():
    # Power cut: the Pi boots hours behind, so a 3h-old queued command looks like it is from the future
    b = backlog(cutoff=10)
    assert b.stale(msg_update(10, -3 * 3600)) == "message"
    assert b.stale(msg_update(10, 30)) is None             # sent during a quick restart: runs
    assert backlog().stale(msg_update(10, -3 * 3600)) is None   # not queued: just a clock skew


async def test_too_old_tap_is_reported_in_the_chat(as_update):
    from telegram.error import BadRequest
    u = tap_update(3)
    u.callback_query.answer.side_effect = BadRequest("Query is too old")
    u.effective_chat = SimpleNamespace(id=1)
    c = gate_context(backlog(cutoff=5))
    with pytest.raises(ApplicationHandlerStop):
        await bl.gate(u, c)
    c.bot.send_message.assert_awaited_once_with(1, bl.OFFLINE_TAP)


async def test_failed_notice_still_drops_the_update(as_update):
    u = tap_update(3)
    c = gate_context(backlog(cutoff=5))
    c.application.bot_data["ctx"].users.is_allowed = lambda uid: 1 / 0
    with pytest.raises(ApplicationHandlerStop):
        await bl.gate(u, c)


@pytest.fixture
def as_update(monkeypatch):
    # The gate only acts on real Update objects; let the fakes pass
    monkeypatch.setattr(bl, "Update", SimpleNamespace)


async def test_gate_answers_and_stops_stale_tap(as_update):
    u = tap_update(3)
    with pytest.raises(ApplicationHandlerStop):
        await bl.gate(u, gate_context(backlog(cutoff=5)))
    u.callback_query.answer.assert_awaited_once_with(bl.OFFLINE_TAP, show_alert=True)


async def test_gate_tells_user_how_old_the_message_was(as_update):
    u = msg_update(1, 3600)
    with pytest.raises(ApplicationHandlerStop):
        await bl.gate(u, gate_context(backlog()))
    (text,), _ = u.message.reply_text.call_args
    assert text.startswith("Not run: you sent this 60 min ago")


async def test_gate_silent_for_strangers(as_update):
    u = tap_update(3, user_id=99)
    with pytest.raises(ApplicationHandlerStop):
        await bl.gate(u, gate_context(backlog(cutoff=5)))
    u.callback_query.answer.assert_not_awaited()


async def test_gate_lets_fresh_updates_through(as_update):
    assert await bl.gate(msg_update(1, 5), gate_context(backlog(cutoff=0))) is None
    assert await bl.gate(tap_update(6), gate_context(backlog(cutoff=5))) is None


def test_gate_runs_before_every_handler(tmp_path):
    from iotbot.bot.app import build_application
    from tests.test_ac_service import make_ctx

    app = build_application(make_ctx(tmp_path))
    (first,) = app.handlers[-1]
    assert isinstance(first, TypeHandler) and first.type is Update and first.callback is bl.gate
    assert isinstance(app.bot_data["backlog"], bl.Backlog)


# ---- token in logs ---------------------------------------------------------------------

def record(msg, *args, exc=None):
    return logging.LogRecord("telegram.ext", logging.ERROR, __file__, 1, msg, args, exc)


def test_redact_message_args_and_traceback():
    f = RedactFilter("602:" + SECRET)
    r = record("polling https://api.telegram.org/bot602:%s/getMe", SECRET)
    f.filter(r)
    assert SECRET not in r.getMessage() and "bot602:<token>/getMe" in r.getMessage()
    try:
        raise ValueError(f"The token `602:{SECRET}` was rejected by the server.")
    except ValueError:
        import sys
        r = record("Network loop error", exc=sys.exc_info())
    f.filter(r)
    out = logging.Formatter().format(r)
    assert SECRET not in out and "602:<token>" in out
    r = record("x")
    r.stack_info = f"Stack: bot602:{SECRET}"
    f.filter(r)
    assert SECRET not in r.stack_info


def test_short_secret_never_scrubbed():
    r = record("x marks the spot")
    RedactFilter("1:x").filter(r)
    assert r.getMessage() == "x marks the spot"
