"""Broadlink IR packets: mark/space durations <-> the 0x26 packet an RM remote sends.

Every generated IR code ends here, whatever made the durations (iotbot/ac/mitsubishi.py for the
aircons, iotbot/ir/rc6.py for Philips TVs). Packet: 0x26, repeat count, little-endian payload length,
then one duration per byte in ticks, or 0x00 + 2 bytes big-endian when it does not fit in one. The
last duration is the long final space; python-broadlink pads the packet itself.

The tick is a parameter on purpose. 8192/269 = 30.45 us is what the hardware does: a real Philips
remote's RC6 capture decodes to the RC6 spec with it (leader 88 ticks = 2680 us, spec 2667 us).
The Mitsubishi encoder keeps the 32.84 us that python-broadlink documents: its durations were
written for that unit and every aircon accepts them, so its bytes stay exactly as they are
(tests/test_ac_golden.py pins them). Do not "unify" the two.
"""

from __future__ import annotations

import math

IR_PACKET = 0x26
TICK_US = 8192 / 269                    # 30.45 us, measured (see above)


class PacketError(ValueError):
    pass


def to_packet(pulses: list[int] | list[float], tick_us: float = TICK_US, repeat: int = 0) -> bytes:
    """Broadlink IR packet for mark/space `pulses` (us, starting with a mark). Rounds to ticks;
    broadlink's own pulses_to_data truncates, which shortens every duration by up to one tick."""
    if not 0 <= repeat <= 0xFF:
        raise PacketError(f"repeat {repeat} is out of range")
    body = bytearray()
    for us in pulses:
        if not math.isfinite(us):
            raise PacketError(f"pulse {us} is not a duration")
        ticks = max(1, round(us / tick_us))
        if ticks > 0xFFFF:
            raise PacketError(f"pulse {us} us is too long for a Broadlink packet")
        if ticks > 0xFF:
            body += bytes((0, ticks >> 8, ticks & 0xFF))
        else:
            body.append(ticks)
    if len(body) > 0xFFFF:
        raise PacketError("signal is too long for a Broadlink packet")
    return bytes((IR_PACKET, repeat, len(body) & 0xFF, len(body) >> 8)) + bytes(body)


def to_pulses(packet: bytes, tick_us: float = TICK_US) -> list[int]:
    """Durations (us) in a Broadlink IR packet; padding after the length is ignored.
    Raises PacketError if malformed."""
    if len(packet) < 4 or packet[0] != IR_PACKET:
        raise PacketError("not a Broadlink IR packet")
    end = 4 + (packet[2] | packet[3] << 8)
    if end > len(packet):
        raise PacketError("Broadlink packet is shorter than its length field")
    out, i = [], 4
    while i < end:
        ticks = packet[i]
        i += 1
        if ticks == 0:
            if i + 2 > end:
                raise PacketError("Broadlink packet ends inside a long pulse")
            ticks = packet[i] << 8 | packet[i + 1]
            i += 2
        out.append(round(ticks * tick_us))
    return out


def to_ticks(packet: bytes) -> list[int]:
    """Raw tick counts in a Broadlink IR packet (for comparing packets without a tick assumption)."""
    return [round(us / 1000) for us in to_pulses(packet, tick_us=1000)]
