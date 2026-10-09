"""Mitsubishi Electric 144-bit AC protocol: AcState <-> 18-byte frame <-> Broadlink packet.

Byte layout from IRremoteESP8266 `ir_Mitsubishi.h`, checked against our captures
(commands.json `mitsubishi`) and two real-remote vectors from its tests:

    b0-4   23 cb 26 01 00   fixed
    b5     0x20             power on
    b6     mode << 3        cool = 3 -> 0x18
    b7     temp - 16
    b8     0x36             cool: wide vane middle (3 << 4) | cool flag 0x06
    b9     fan bits 0-2 | vane bits 3-5 | 0x40 (always set by these remotes)
    b10-14 clock and timers, unused (0)
    b15    0x08             powerful (unnamed bit upstream; from our captures)
    b16    0
    b17    sum(b0..b16) & 0xff

Fan auto is raw 0 without IRremoteESP8266's FanAuto bit (0x80): our powerful
captures and the upstream cool-mode capture both send it that way.
Bits go out LSB first; the remote sends the frame twice.
"""

from __future__ import annotations

from iotbot.ac.state import AcState

HEADER = bytes.fromhex("23cb260100")
FRAME_LEN = 18

POWER_ON = 0x20
MODE_BYTE = {"cool": 0x18}
B8 = {"cool": 0x36}
VANE_FLAG = 0x40
POWERFUL = 0x08
# Quiet goes on the wire as raw 5 (upstream calls it Silent = 6 but stores 6 - 1)
FAN_RAW = {"auto": 0, 1: 1, 2: 2, 3: 3, 4: 4, "quiet": 5}
VANE_RAW = {"auto": 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, "swing": 7}
RAW_FAN = {v: k for k, v in FAN_RAW.items()}
RAW_VANE = {v: k for k, v in VANE_RAW.items()}
RAW_MODE = {v: k for k, v in MODE_BYTE.items()}

# Timings in microseconds: averages measured from our captures (IRremoteESP8266
# nominal values are 3400/1750/450/1300/420, within the receiver's tolerance)
HDR_MARK, HDR_SPACE = 3700, 1800
BIT_MARK, ONE_SPACE, ZERO_SPACE = 500, 1320, 415
FRAME_GAP = 13700
# Broadlink end-of-signal gap, as in every capture (0x0d05 ticks)
END_GAP = 109455
COPIES = 2

# Broadlink IR packet: 0x26, repeat count, little-endian payload length, then
# durations in ticks of 269/8192 ms (32.84 us); one byte, or 0x00 + 2 bytes big-endian
IR_PACKET = 0x26
TICK_US = 32.84


class AcFrameError(ValueError):
    pass


def checksum(data: bytes) -> int:
    return sum(data[:FRAME_LEN - 1]) & 0xFF


def encode(state: AcState) -> bytes:
    """The 18-byte frame for `state`."""
    b = bytearray(FRAME_LEN)
    b[0:5] = HEADER
    b[5] = POWER_ON if state.power else 0
    b[6] = MODE_BYTE[state.mode]
    b[7] = state.temp - 16
    b[8] = B8[state.mode]
    b[9] = FAN_RAW[state.fan] | VANE_RAW[state.vane] << 3 | VANE_FLAG
    b[15] = POWERFUL if state.powerful else 0
    b[17] = checksum(b)
    return bytes(b)


def decode(frame: bytes) -> AcState:
    """The AcState in `frame`. Raises AcFrameError for a bad frame or one with
    settings this bot does not model (other modes, timers, ...), so a decoded
    state always encodes back to the same bytes."""
    if len(frame) != FRAME_LEN:
        raise AcFrameError(f"frame is {len(frame)} bytes, expected {FRAME_LEN}")
    if frame[:5] != HEADER:
        raise AcFrameError(f"not a Mitsubishi 144-bit frame (header {frame[:5].hex()})")
    if checksum(frame) != frame[17]:
        raise AcFrameError(f"bad checksum {frame[17]:02x}, expected {checksum(frame):02x}")
    mode = RAW_MODE.get(frame[6])
    fan = RAW_FAN.get(frame[9] & 0x07)
    vane = RAW_VANE.get(frame[9] >> 3 & 0x07)
    if mode is None or fan is None or vane is None:
        raise AcFrameError(f"unsupported mode/fan/vane in {frame.hex(' ')}")
    try:
        state = AcState(power=frame[5] == POWER_ON, temp=(frame[7] & 0x0F) + 16, fan=fan, vane=vane,
                        mode=mode, powerful=frame[15] == POWERFUL)
    except ValueError as e:
        raise AcFrameError(str(e)) from None
    if encode(state) != frame:
        raise AcFrameError(f"frame has settings this bot does not model: {frame.hex(' ')}")
    return state


def frame_to_pulses(frame: bytes, copies: int = COPIES) -> list[int]:
    """Mark/space durations (us) for `frame` sent `copies` times."""
    pulses: list[int] = []
    for i in range(copies):
        pulses += [HDR_MARK, HDR_SPACE]
        for byte in frame:
            for bit in range(8):
                pulses += [BIT_MARK, ONE_SPACE if byte >> bit & 1 else ZERO_SPACE]
        pulses += [BIT_MARK, FRAME_GAP if i < copies - 1 else END_GAP]
    return pulses


def pulses_to_packet(pulses: list[int]) -> bytes:
    """Broadlink IR packet for `pulses` (us). Rounds to ticks; broadlink's own
    pulses_to_data truncates, which shortens every duration by up to one tick."""
    body = bytearray()
    for us in pulses:
        ticks = max(1, round(us / TICK_US))
        if ticks > 0xFFFF:
            raise AcFrameError(f"pulse {us} us is too long for a Broadlink packet")
        if ticks > 0xFF:
            body += bytes((0, ticks >> 8, ticks & 0xFF))
        else:
            body.append(ticks)
    return bytes((IR_PACKET, 0, len(body) & 0xFF, len(body) >> 8)) + bytes(body)


def packet_to_pulses(packet: bytes) -> list[int]:
    """Durations (us) in a Broadlink IR packet. Raises AcFrameError if malformed."""
    if len(packet) < 4 or packet[0] != IR_PACKET:
        raise AcFrameError("not a Broadlink IR packet")
    end = 4 + (packet[2] | packet[3] << 8)
    if end > len(packet):
        raise AcFrameError("Broadlink packet is shorter than its length field")
    out, i = [], 4
    while i < end:
        ticks = packet[i]
        i += 1
        if ticks == 0:
            if i + 2 > end:
                raise AcFrameError("Broadlink packet ends inside a long pulse")
            ticks = packet[i] << 8 | packet[i + 1]
            i += 2
        out.append(round(ticks * TICK_US))
    return out


def pulses_to_frames(pulses: list[int]) -> list[bytes]:
    """Frames found in mark/space durations (us). A frame is a header pair, 144 bits,
    a closing mark and a gap; anything that does not fit raises AcFrameError."""
    frames, i, n = [], 0, len(pulses)
    while i < n:
        if i + 2 + 2 * FRAME_LEN * 8 + 2 > n:
            raise AcFrameError(f"{n - i} pulses left at {i}, too few for a frame")
        mark, space = pulses[i], pulses[i + 1]
        if not (2500 <= mark <= 4500 and 1200 <= space <= 2400):
            raise AcFrameError(f"no frame header at pulse {i} ({mark}/{space} us)")
        i += 2
        b = bytearray(FRAME_LEN)
        for bit in range(FRAME_LEN * 8):
            mark, space = pulses[i], pulses[i + 1]
            if not 250 <= mark <= 800:
                raise AcFrameError(f"bad bit mark {mark} us at pulse {i}")
            if 900 <= space <= 1800:
                b[bit // 8] |= 1 << (bit % 8)
            elif not 250 <= space <= 700:
                raise AcFrameError(f"bad bit space {space} us at pulse {i + 1}")
            i += 2
        mark, gap = pulses[i], pulses[i + 1]
        if not 250 <= mark <= 800 or gap < 5000:
            raise AcFrameError(f"frame not closed by a mark and gap at pulse {i} ({mark}/{gap} us)")
        frames.append(bytes(b))
        i += 2
    if not frames:
        raise AcFrameError("no frames in the signal")
    return frames


def build_packet(state: AcState) -> bytes:
    """Broadlink packet that sends `state` the way the remote does (frame twice)."""
    return pulses_to_packet(frame_to_pulses(encode(state)))
