"""Philips RC6 mode 0 (RC6-0-16): address + command + toggle -> mark/space durations (us).

Frame, in units of T = 16 cycles of the 36 kHz carrier (444.4 us):
    leader     mark 6T, space 2T
    start bit  1
    mode       3 bits, 000 for TVs
    toggle     double width (2T + 2T); flips on every new key press
    address    8 bits, MSB first (0 for Philips TVs)
    command    8 bits, MSB first
RC6 Manchester: a 1 is mark then space, a 0 is space then mark (the opposite of RC5).

Checked against a real Philips remote (part HOF14H513GPHFD7, 2014): its Mute press is 5 identical
frames (address 0, command 13, toggle 0), 84 ms from the last mark of one frame to the next leader,
ending with the usual 0x0d05-tick space (tests/fixtures/ir/philips_rc6_mute.hex, tests/test_ir.py).
One frame was enough for that TV (2026-11-21); more frames from the same press keep one toggle value,
which the TV reads as a held key, not a second press.
"""

from __future__ import annotations

from iotbot.ir.packet import TICK_US

T_US = 16 / 36000 * 1_000_000           # 444.44 us
REPEAT_GAP_US = 84_050                  # last mark -> next leader, as the real remote sends
FINAL_GAP_US = 3333 * TICK_US           # = 0x0d05 ticks, the end space every capture has
BODY_HALVES = 2 + 6 + 4 + 32            # start, mode, toggle (double), address + command


def _levels(address: int, command: int, toggle: int) -> list[tuple[bool, float]]:
    for name, v, top in (("address", address, 0xFF), ("command", command, 0xFF), ("toggle", toggle, 1)):
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= top:
            raise ValueError(f"RC6 {name} must be 0..{top}, got {v!r}")
    t = T_US
    lv: list[tuple[bool, float]] = [(True, 6 * t), (False, 2 * t)]

    def bit(b: int, width: float = t) -> None:
        lv.extend([(True, width), (False, width)] if b else [(False, width), (True, width)])

    bit(1)                                                    # start
    for _ in range(3):
        bit(0)                                                # mode 000
    bit(toggle, 2 * t)
    for i in range(7, -1, -1):
        bit(address >> i & 1)
    for i in range(7, -1, -1):
        bit(command >> i & 1)
    return lv


def frame(address: int, command: int, toggle: int, gap_us: float = FINAL_GAP_US) -> list[float]:
    """One frame: durations starting with the leader mark, ending with `gap_us` of space after the
    last mark. Raises ValueError for an address/command outside 0..255 or a toggle other than 0/1."""
    merged: list[list] = []
    for mark, us in _levels(address, command, toggle):
        if merged and merged[-1][0] == mark:
            merged[-1][1] += us
        else:
            merged.append([mark, us])
    if not merged[-1][0]:
        merged.pop()                                          # a trailing half-bit space joins the gap
    return [us for _, us in merged] + [gap_us]               # rounded once, to ticks, by packet.py


def press(address: int, command: int, toggle: int, frames: int = 1) -> list[float]:
    """One key press: `frames` identical frames (one toggle value), REPEAT_GAP_US apart."""
    if not 1 <= frames <= 10:
        raise ValueError(f"frames must be 1..10, got {frames}")
    out: list[float] = []
    for i in range(frames):
        out += frame(address, command, toggle, REPEAT_GAP_US if i < frames - 1 else FINAL_GAP_US)
    return out


def decode(pulses: list[float], t_us: float = T_US) -> list[tuple[int, int, int]]:
    """(address, command, toggle) for each RC6 mode 0 frame in `pulses` (us). Raises ValueError for
    anything else. Used by tests and to identify a learned capture."""
    if not pulses:
        raise ValueError("no signal")
    out = []
    frames, cur = [], []
    for i, us in enumerate(pulses):
        if i % 2 and us > 20 * t_us:                         # a long space ends a frame
            frames.append(cur)
            cur = []
        else:
            cur.append(us)
    if cur:
        frames.append(cur)
    for fr in frames:
        halves = []
        for i, us in enumerate(fr):
            n = round(us / t_us)
            if n < 1 or abs(us - n * t_us) > t_us * 0.4:
                raise ValueError(f"duration {us} us is not a whole number of RC6 half-bits")
            halves += [i % 2 == 0] * n
        if len(halves) < 10 or halves[:8] != [True] * 6 + [False] * 2:
            raise ValueError("no RC6 leader")
        body = halves[8:]
        if len(body) == BODY_HALVES - 1:
            body.append(False)                                # the last half-bit space is in the gap
        if len(body) != BODY_HALVES:
            raise ValueError(f"RC6 mode 0 frame has {len(body)} half-bits after the leader, "
                             f"expected {BODY_HALVES}")

        def read(pos: int, width: int = 1) -> int:
            a = body[pos]
            if body[pos:pos + width] != [a] * width or body[pos + width:pos + 2 * width] != [not a] * width:
                raise ValueError(f"not Manchester at half-bit {pos}")
            return 1 if a else 0

        if read(0) != 1:
            raise ValueError("RC6 start bit is not 1")
        mode = read(2) << 2 | read(4) << 1 | read(6)
        if mode != 0:
            raise ValueError(f"RC6 mode {mode}, only mode 0 is supported")
        toggle = read(8, 2)
        bits = [read(12 + 2 * i) for i in range(16)]
        address = int("".join(map(str, bits[:8])), 2)
        command = int("".join(map(str, bits[8:])), 2)
        out.append((address, command, toggle))
    return out
