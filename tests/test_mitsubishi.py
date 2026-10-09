import broadlink.remote
import pytest

from iotbot.ac import mitsubishi as m
from iotbot.ac.state import AcState

COOL24 = AcState(power=True, temp=24, fan=2, vane="auto")


def test_encode_layout():
    f = m.encode(COOL24)
    assert f.hex(" ") == "23 cb 26 01 00 20 18 08 36 42 00 00 00 00 00 00 00 cd"
    assert m.checksum(f) == f[17]


def test_fan_vane_powerful_bits():
    f = m.encode(COOL24.with_changes(fan="quiet", vane="swing", powerful=True, power=False))
    assert f[5] == 0
    assert f[9] == 5 | 7 << 3 | 0x40
    assert f[15] == 0x08
    assert m.encode(COOL24.with_changes(fan="auto"))[9] & 0x87 == 0  # no FanAuto bit
    assert m.encode(COOL24.with_changes(fan="quiet"))[9] & 0x07 == 5  # not upstream's 6


def test_decode_round_trip_every_state():
    n = 0
    for temp in range(16, 32):
        for fan in ("auto", 1, 2, 3, 4, "quiet"):
            for vane in ("auto", 1, 2, 3, 4, 5, "swing"):
                for power in (True, False):
                    for powerful in (True, False):
                        s = AcState(power=power, temp=temp, fan=fan, vane=vane, powerful=powerful)
                        assert m.decode(m.encode(s)) == s
                        n += 1
    assert n == 16 * 6 * 7 * 4


@pytest.mark.parametrize("index,value,match", [
    (17, 0x00, "checksum"),
    (0, 0x24, "header"),
    (6, 0x08, "unsupported"),     # heat mode
    (9, 0x46, "unsupported"),     # raw fan 6
    (9, 0x73, "unsupported"),     # vane 6
    (11, 0x05, "does not model"),  # a timer
    (7, 0x1f, "does not model"),  # half-degree bit
    (15, 0x01, "does not model"),  # direct/indirect
])
def test_decode_rejects(index, value, match):
    f = bytearray(m.encode(COOL24))
    f[index] = value
    if index != 17:
        f[17] = m.checksum(f)
    with pytest.raises(m.AcFrameError, match=match):
        m.decode(bytes(f))


def test_decode_rejects_wrong_length():
    with pytest.raises(m.AcFrameError, match="bytes"):
        m.decode(m.encode(COOL24)[:17])


def test_pulses_shape():
    p = m.frame_to_pulses(m.encode(COOL24))
    assert len(p) == 2 * (2 + 144 * 2 + 2)
    assert p[:2] == [m.HDR_MARK, m.HDR_SPACE]
    assert p[291] == m.FRAME_GAP and p[-1] == m.END_GAP
    # LSB first: 0x23 = 0b00100011 -> bits 1,1,0,0,0,1,0,0
    spaces = p[3:19:2]
    assert spaces == [m.ONE_SPACE if b else m.ZERO_SPACE for b in (1, 1, 0, 0, 0, 1, 0, 0)]


def test_packet_round_trip_and_broadlink_agrees():
    pkt = m.build_packet(COOL24)
    assert pkt[0] == 0x26 and pkt[1] == 0
    assert len(pkt) - 4 == pkt[2] | pkt[3] << 8
    ours = m.packet_to_pulses(pkt)
    theirs = broadlink.remote.data_to_pulses(pkt)
    assert len(ours) == len(theirs) == 584
    assert all(abs(a - b) <= 1 for a, b in zip(ours, theirs))
    assert m.pulses_to_frames(ours) == [m.encode(COOL24)] * 2


def test_ticks_round_not_truncate():
    pkt = m.pulses_to_packet([420, 109455])
    assert pkt[4] == 13                   # 420 / 32.84 = 12.8 -> 13, broadlink would give 12
    assert pkt[5:8] == bytes((0, 0x0D, 0x05))
    assert m.pulses_to_packet([8390, 8400])[4:] == bytes((0xFF, 0, 1, 0))  # 255 vs 256 ticks
    assert m.pulses_to_packet([m.FRAME_GAP])[4:] == bytes((0, 1, 161))  # 13.7 ms = 417 ticks


def test_packet_parse_errors():
    with pytest.raises(m.AcFrameError):
        m.packet_to_pulses(b"\xb2\x00\x01\x00\x10")
    with pytest.raises(m.AcFrameError):
        m.packet_to_pulses(b"\x26\x00\x09\x00\x10")
    with pytest.raises(m.AcFrameError):
        m.packet_to_pulses(b"\x26\x00\x02\x00\x00\x0d")


def test_pulses_to_frames_rejects_noise():
    p = m.frame_to_pulses(m.encode(COOL24), copies=1)
    with pytest.raises(m.AcFrameError, match="header"):
        m.pulses_to_frames([9000, 4500] + p[2:])
    bad = list(p)
    bad[3] = 3000
    with pytest.raises(m.AcFrameError, match="space"):
        m.pulses_to_frames(bad)
    with pytest.raises(m.AcFrameError, match="too few"):
        m.pulses_to_frames(p[:100])
    with pytest.raises(m.AcFrameError, match="too few"):
        m.pulses_to_frames(p[:-1])
    short_gap = list(p)
    short_gap[-1] = 1000
    with pytest.raises(m.AcFrameError, match="not closed"):
        m.pulses_to_frames(short_gap)
    with pytest.raises(m.AcFrameError, match="no frames"):
        m.pulses_to_frames([])
