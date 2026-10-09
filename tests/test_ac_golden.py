"""Golden tests: the real remote captures in commands.json, byte for byte.

There is no hardware check before deploy, so these are what stands between a
wrong bit and an AC that ignores the bot.
"""

import json
from pathlib import Path

import broadlink.remote
import pytest

from iotbot.ac import mitsubishi as m
from iotbot.ac.state import AcState

CAPTURES = json.loads((Path(__file__).parent.parent / "commands.json").read_text())["1"]["mitsubishi"]

BEDROOM = AcState(power=True, temp=22, fan=3, vane="auto")
OFFICE = AcState(power=True, temp=23, fan=3, vane=1)

GOLDEN = {
    ("fn18ve-22", "power_on"): ("23 cb 26 01 00 20 18 06 36 43 00 00 00 00 00 00 00 cc", BEDROOM),
    ("fn18ve-22", "power_off"): ("23 cb 26 01 00 00 18 06 36 43 00 00 00 00 00 00 00 ac",
                                 BEDROOM.with_changes(power=False)),
    ("fn18ve-22", "powerful"): ("23 cb 26 01 00 20 18 06 36 40 00 00 00 00 00 08 00 d1",
                                BEDROOM.with_changes(fan="auto", powerful=True)),
    ("fn18ve-23", "power_on"): ("23 cb 26 01 00 20 18 07 36 4b 00 00 00 00 00 00 00 d5", OFFICE),
    ("fn18ve-23", "power_off"): ("23 cb 26 01 00 00 18 07 36 4b 00 00 00 00 00 00 00 b5",
                                 OFFICE.with_changes(power=False)),
    ("fn18ve-23", "powerful"): ("23 cb 26 01 00 20 18 07 36 48 00 00 00 00 00 08 00 da",
                                OFFICE.with_changes(fan="auto", powerful=True)),
}

# Real-remote vectors from IRremoteESP8266 test/ir_Mitsubishi_test.cpp (same cool-mode family):
# TestDecodeMitsubishiAC.Issue891 and TestDecodeMitsubishiAC.DecodeRealExample
UPSTREAM = {
    "23 cb 26 01 00 00 18 08 36 40 00 00 00 00 00 00 00 ab": AcState(power=False, temp=24),
    "23 cb 26 01 00 00 18 0a 36 79 00 00 00 00 00 00 00 e6": AcState(power=False, temp=26, fan=1,
                                                                       vane="swing"),
}

# Per-pulse tolerance against the real remote. Measured worst: 131 us on a header,
# 66 us (2 ticks) on bits. IR receivers typically accept 25% or more.
LONG_TOLERANCE_US = 160   # header and gaps
BIT_TOLERANCE_US = 100    # bit marks and spaces (100 us is ~24% of a zero space)


def test_every_capture_is_covered():
    assert {(model, key) for model, feats in CAPTURES.items() for key in feats} == set(GOLDEN)


@pytest.mark.parametrize("model,key", sorted(GOLDEN))
def test_capture_frames(model, key):
    frames = m.pulses_to_frames(m.packet_to_pulses(bytes.fromhex(CAPTURES[model][key])))
    expected, _ = GOLDEN[model, key]
    assert [f.hex(" ") for f in frames] == [expected] * 2


@pytest.mark.parametrize("model,key", sorted(GOLDEN))
def test_decode_and_encode_byte_exact(model, key):
    frame_hex, state = GOLDEN[model, key]
    frame = bytes.fromhex(frame_hex)
    assert m.checksum(frame) == frame[17]
    assert m.decode(frame) == state
    assert m.encode(state) == frame


@pytest.mark.parametrize("model,key", sorted(GOLDEN))
def test_generated_packet_matches_capture_timing(model, key):
    _, state = GOLDEN[model, key]
    ours = broadlink.remote.data_to_pulses(m.build_packet(state))
    real = broadlink.remote.data_to_pulses(bytes.fromhex(CAPTURES[model][key]))
    assert len(ours) == len(real) == 584
    for i, (a, b) in enumerate(zip(ours, real)):
        limit = LONG_TOLERANCE_US if a > 2000 or b > 2000 else BIT_TOLERANCE_US
        assert abs(a - b) <= limit, (i, a, b)
    # Marks and spaces line up: same bit everywhere, not just close in total
    assert all((a > 900) == (b > 900) for a, b in zip(ours, real))


@pytest.mark.parametrize("model,key", sorted(GOLDEN))
def test_packet_header_matches_capture(model, key):
    # 0x26 IR, repeat 0, same payload length; data_to_pulses ignores the repeat byte
    _, state = GOLDEN[model, key]
    assert m.build_packet(state)[:4] == bytes.fromhex(CAPTURES[model][key])[:4]


@pytest.mark.parametrize("frame_hex", sorted(UPSTREAM))
def test_upstream_real_remote_vectors(frame_hex):
    frame = bytes.fromhex(frame_hex)
    assert m.decode(frame) == UPSTREAM[frame_hex]
    assert m.encode(UPSTREAM[frame_hex]) == frame
