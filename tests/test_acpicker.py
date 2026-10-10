import pytest

from iotbot.ac.state import FANS, VANES, AcState
from iotbot.bot import acpicker as ap
from iotbot.bot.callbacks import decode

BED = AcState(power=True, temp=22, fan=3, vane="auto")


def test_state_code_round_trip_every_state():
    for temp in range(16, 32):
        for fan in FANS:
            for vane in VANES:
                for powerful in (False, True):
                    st = AcState(power=True, temp=temp, fan=fan, vane=vane, powerful=powerful)
                    code = ap.encode_state(st)
                    assert len(code) == 5 and ap.decode_state(code) == st


@pytest.mark.parametrize("code", ["", "2231", "15aa0", "32aa0", "22xa0", "22ax0", "22aa2", "ab3a0", "223a00"])
def test_bad_codes(code):
    assert ap.decode_state(code) is None


def test_ops():
    assert ap.apply_op(BED, "t+", BED).temp == 23
    assert ap.apply_op(BED.with_changes(temp=31), "t+", BED).temp == 31
    assert ap.apply_op(BED.with_changes(temp=16), "t-", BED).temp == 16
    assert ap.apply_op(BED, "fq", BED).fan == "quiet"
    assert ap.apply_op(BED, "vs", BED).vane == "swing"
    assert ap.apply_op(BED, "p", BED).powerful
    changed = BED.with_changes(temp=27, fan=1, powerful=True)
    assert ap.apply_op(changed, "r", BED) == BED
    assert ap.apply_op(BED, "zz", BED) == BED and ap.apply_op(BED, "f9", BED) == BED


def test_keyboard_marks_and_fits():
    kb = ap.picker_keyboard("d1a2b3c", BED)
    labels = [[b.text for b in row] for row in kb.inline_keyboard]
    assert labels[0] == ["-", "22C", "+"]
    assert "[3]" in labels[1] and "[vane auto]" in labels[2]
    for row in kb.inline_keyboard:
        for b in row:
            assert len(b.callback_data.encode()) <= 64
            ns, owner, code, op = decode(b.callback_data)
            assert (ns, owner, ap.decode_state(code)) == ("ap", "d1a2b3c", BED)


def test_text():
    assert ap.picker_text("bed_ac", BED.with_changes(powerful=True)).startswith(
        "bed_ac: cool 22C fan 3 vane auto powerful")
