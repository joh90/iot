"""Phase 10: generated IR (iotbot/ir). Pure functions; nothing here reaches the LAN."""

from pathlib import Path

import broadlink.remote
import pytest

from iotbot.ir import packet, rc6

MUTE = bytes.fromhex((Path(__file__).parent / "fixtures" / "ir" / "philips_rc6_mute.hex").read_text().strip())


# ---- packet ----------------------------------------------------------------------------------------

def test_packet_round_trip_long_form_and_end():
    pkt = packet.to_packet([2667, 889, 444, 101_501])
    assert pkt[:4] == bytes((0x26, 0, 6, 0))                    # 3 one-byte durations + one long form
    assert pkt[4:] == bytes((88, 29, 15, 0, 0x0D, 0x05))         # the end space is 0x0d05 ticks
    assert len(pkt) == 4 + (pkt[2] | pkt[3] << 8)                 # no padding: python-broadlink pads
    assert packet.to_ticks(pkt) == [88, 29, 15, 3333]
    assert [abs(a - b) <= 16 for a, b in zip(packet.to_pulses(pkt), [2667, 889, 444, 101_501])] == [True] * 4


def test_packet_one_byte_limit_and_repeat():
    t = packet.TICK_US
    assert packet.to_packet([255 * t, 256 * t])[4:] == bytes((0xFF, 0, 1, 0))
    assert packet.to_packet([444], repeat=2)[1] == 2
    with pytest.raises(packet.PacketError):
        packet.to_packet([444], repeat=256)
    with pytest.raises(packet.PacketError, match="too long"):
        packet.to_packet([70_000 * t])


def test_packet_parse_errors_and_padding_ignored():
    assert packet.to_ticks(MUTE)[-1] == 3333 and len(MUTE) > 4 + (MUTE[2] | MUTE[3] << 8)   # padded capture
    for bad, why in ((b"\xb2\x00\x01\x00\x10", "not a Broadlink IR"), (b"\x26\x00\x09\x00\x10", "shorter"),
                     (b"\x26\x00\x02\x00\x00\x0d", "inside a long")):
        with pytest.raises(packet.PacketError, match=why):
            packet.to_pulses(bad)


# ---- RC6 -------------------------------------------------------------------------------------------

def test_the_real_remotes_mute_decodes_to_address_0_command_13():
    assert rc6.decode(packet.to_pulses(MUTE)) == [(0, 13, 0)] * 5


def test_generated_mute_matches_the_real_remote_tick_for_tick():
    ours = packet.to_ticks(packet.to_packet(rc6.press(0, 13, 0, frames=5)))
    real = packet.to_ticks(MUTE)
    assert len(ours) == len(real) == 200
    # the remote itself varies by up to 3 ticks between its own frames (leader 88-90, gaps 2757-2761)
    assert max(abs(a - b) for a, b in zip(ours, real)) <= 3


def test_one_frame_press_ends_like_a_capture_and_broadlink_reads_it():
    pkt = packet.to_packet(rc6.press(0, 13, 1))
    assert pkt.endswith(bytes((0, 0x0D, 0x05))) and len(pkt) == 4 + (pkt[2] | pkt[3] << 8)
    assert len(broadlink.remote.data_to_pulses(pkt)) == len(packet.to_pulses(pkt))
    assert rc6.decode(packet.to_pulses(pkt)) == [(0, 13, 1)]


@pytest.mark.parametrize("address,command,toggle", [
    (0, 0, 0), (0, 0xFF, 1), (0xFF, 0, 0), (0xFF, 0xFF, 1), (0, 12, 0), (0, 12, 1), (0x80, 0x01, 0),
    (0, 61, 1), (0, 63, 0), (0x5A, 0xA5, 1),
])
def test_round_trip_edges(address, command, toggle):
    assert rc6.decode(rc6.press(address, command, toggle, frames=2)) == [(address, command, toggle)] * 2


def test_toggle_is_double_width_and_flips_the_halves():
    t = rc6.T_US
    one, zero = rc6.frame(0, 0, 1), rc6.frame(0, 0, 0)
    # leader 6T, 2T space, start mark 1T, then mode 000 = space-mark x3: space T + ... ; toggle 1 after
    # the last mode mark makes a 3T mark (1T + 2T)
    assert round(one[0] / t) == 6 and round(one[1] / t) == 2
    assert 3 in [round(d / t) for d in one] and one != zero


def test_frame_rejects_bad_values():
    for args in ((256, 0, 0), (0, -1, 0), (0, 0, 2), (True, 0, 0), (0, 1.0, 0)):
        with pytest.raises(ValueError):
            rc6.frame(*args)
    with pytest.raises(ValueError):
        rc6.press(0, 0, 0, frames=0)


def test_decode_rejects_other_signals():
    with pytest.raises(ValueError):
        rc6.decode([9000, 4500, 560, 560, 560, 108_000])            # NEC leader
    t = rc6.T_US
    for bad in (rc6.frame(0, 13, 0)[:11] + [100_000],              # cut short, ends with a gap
                [6 * t, 2 * t, t, 2 * t, 2 * t, 100_000],             # leader + a few bits (was IndexError)
                rc6.frame(0, 13, 0)[:-1] + [t, t, t, 100_000],        # two extra bits after the command
                [], [6 * t, 2 * t, t, t, t, 3 * t, t, 100_000]):      # empty; toggle not Manchester
        with pytest.raises(ValueError):
            rc6.decode(bad)


def test_codeset_protocol_without_an_encoder_raises_so_the_registry_warns():
    from iotbot.ir.codesets import Button, Codeset, problems

    cs = Codeset("x", "nec", 0, {"power_on": Button(1, True)})
    with pytest.raises(ValueError, match="no encoder"):
        cs.packets("power_on")
    assert problems(Codeset("Bad Name", "rc6", 0, {"Power-On": Button(1, True)}, frames=0)) == [
        "Bad Name: codeset names are lowercase (devices.json lookup is case-insensitive)",
        "Bad Name: frames 0 out of range", "Bad Name: bad button name 'Power-On'"]


def test_packet_rejects_non_finite():
    with pytest.raises(packet.PacketError):
        packet.to_packet([float("nan")])
