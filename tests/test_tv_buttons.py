"""Phase 10.6: a generated-IR TV on the button surfaces. A toggle never runs unattended."""

import pytest

from iotbot.bot import oneliner
from iotbot.bot.wizard import Wizard
from iotbot.devices.model import schedulable_key
from iotbot.ir.codesets import CODESETS, Button, Codeset
from iotbot.schedule.model import Capture, Off
from tests.test_ac_service import make_ctx


@pytest.fixture
def toggle_tv(monkeypatch):
    monkeypatch.setitem(CODESETS, "test-toggle", Codeset("test-toggle", "rc6", 0, {"power": Button(12, False)}))
    return "test-toggle"


def wizard(ctx):
    return Wizard(ctx)


def test_discrete_tv_has_timers_and_schedules(tmp_path):
    ctx = make_ctx(tmp_path, tv="philips-rc6")
    assert all("bed_tv" not in w or "not yet checked" in w for w in ctx.registry.warnings)
    w = wizard(ctx)
    assert "bed_tv" in w.schedulable() and w.can_timer("bed_tv")
    tv = ctx.devices.get("bed_tv")
    assert schedulable_key(tv, "power_on") and schedulable_key(tv, "power_off")


def test_toggle_only_tv_gets_no_timer_no_schedule_and_no_on_off(tmp_path, toggle_tv):
    ctx = make_ctx(tmp_path, tv=toggle_tv)
    w = wizard(ctx)
    tv = ctx.devices.get("bed_tv")
    assert list(tv.features) == ["power"] and not schedulable_key(tv, "power")
    assert "bed_tv" not in w.schedulable() and not w.can_timer("bed_tv")
    assert ctx.devices.resolve("bed_tv", ["off"]).error == "feature_not_found"


def test_schedule_service_calls_a_toggle_schedule_broken(tmp_path, toggle_tv):
    ctx = make_ctx(tmp_path, tv=toggle_tv)
    s = ctx.schedules
    sched = type("S", (), {"device": "bed_tv", "action": Capture("power")})()
    assert "is a toggle and cannot be scheduled" in s.problem(sched)
    sched.action = Off()
    assert s.problem(sched) == "bed_tv has no power off"


def test_learned_devices_keep_their_old_schedule_behaviour(tmp_path):
    ctx = make_ctx(tmp_path)
    old = ctx.devices.get("old_ac")                     # daikin, learned captures, no codeset
    assert not old.codeset and schedulable_key(old, "power_on")


def test_one_liner_refuses_a_toggle_and_room_word_skips_it(tmp_path, toggle_tv):
    ctx = make_ctx(tmp_path, tv=toggle_tv)
    w = wizard(ctx)
    with pytest.raises(oneliner.LineError, match="toggle"):
        oneliner.parse_add(w, 1, "bed_tv power 23:00")
    # "bedroom off 23:00" can only mean the aircon: the toggle TV does not fit
    assert oneliner.resolve_device(w, "bedroom", "off") == "bed_ac"


def test_one_liner_discrete_tv_timer(tmp_path):
    w = wizard(make_ctx(tmp_path, tv="philips-rc6"))
    d = oneliner.parse_add(w, 1, "bed_tv off 23:00")
    assert d.device == "bed_tv" and d.action == {"kind": "off"}
