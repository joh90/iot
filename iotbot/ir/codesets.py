"""Codesets: which IR codes a family of remotes sends, by button name.

The universal-remote idea: a device in devices.json names its codeset (`"codeset": "philips-rc6"`)
instead of carrying learned captures, the way an aircon names its brand and the Mitsubishi encoder
builds its frames. Everything above this (buttons, timers, chat) only sees button names.

A button is `discrete` when it always means the same thing whatever the device is doing (power on,
power off). A toggle (one power key) is not: sending it twice undoes itself, so it is never resent,
never scheduled, never used by chat, and it is never called power_on / power_off.

A `guarded` button is the middle way, for a TV that only has the toggle: power_on and power_off both
send the toggle, and DeviceService sends one only when the bot's own record of its last power send
(last GUARD_HOURS) does not already say that state. Chat, timers and buttons see power on / off as
usual; the guard is what keeps a second "off" from turning the TV back on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from iotbot.ir import packet, rc6

PROTOCOLS = ("rc6",)


@dataclass(frozen=True, slots=True)
class Button:
    command: int
    discrete: bool = False
    guarded: bool = False        # a toggle used as power_on / power_off, behind the state guard


@dataclass(frozen=True, slots=True)
class Codeset:
    name: str
    protocol: str
    address: int
    buttons: dict[str, Button] = field(default_factory=dict)
    frames: int = 1
    # Checked on a real device: until then the registry warns at every start (a wrong guess could be
    # scheduled or used by chat)
    verified: bool = False

    def packets(self, key: str) -> tuple[bytes, bytes]:
        """Both toggle variants of one press of `key`: (toggle 0, toggle 1). Raises ValueError for
        a protocol this bot cannot encode (the registry turns it into a warning)."""
        if self.protocol != "rc6":
            raise ValueError(f"no encoder for protocol {self.protocol!r}")
        b = self.buttons[key]
        p0, p1 = (packet.to_packet(rc6.press(self.address, b.command, t, self.frames)) for t in (0, 1))
        return p0, p1

    def frame_id(self, key: str, toggle: int) -> str:
        """Short name of what goes out, for the device log: 'rc6:0:61:t1'."""
        return f"{self.protocol}:{self.address}:{self.buttons[key].command}:t{toggle}"


# Philips TVs (TP Vision / TPV, RC6 mode 0, address 0). Checked on the bedroom TV: mute 13 works
# (2026-11-21); the discrete off 61 does nothing and the power toggle 12 turns it off and back on
# from standby (2026-11-22). So power on / off are both the toggle behind the guard (owner's call:
# 12 for both, for good).
CODESETS: dict[str, Codeset] = {
    "philips-rc6": Codeset("philips-rc6", "rc6", 0, {
        "power_on": Button(12, guarded=True),
        "power_off": Button(12, guarded=True),
    }, verified=True),
}


def problems(cs: Codeset) -> list[str]:
    """What is wrong with a codeset definition (tests run this over CODESETS)."""
    out = []
    if cs.name != cs.name.lower():
        out.append(f"{cs.name}: codeset names are lowercase (devices.json lookup is case-insensitive)")
    if not 1 <= cs.frames <= 10:
        out.append(f"{cs.name}: frames {cs.frames} out of range")
    if cs.protocol not in PROTOCOLS:
        out.append(f"{cs.name}: unknown protocol {cs.protocol!r}")
    if not 0 <= cs.address <= 0xFF:
        out.append(f"{cs.name}: address {cs.address} out of range")
    for key, b in cs.buttons.items():
        if not re.fullmatch(r"[a-z0-9_]{1,24}", key):
            out.append(f"{cs.name}: bad button name {key!r}")
        if key in ("power_on", "power_off") and not (b.discrete or b.guarded):
            out.append(f"{cs.name}: {key} must be a discrete or guarded code (a plain toggle is called 'power')")
        if b.guarded and key not in ("power_on", "power_off"):
            out.append(f"{cs.name}: {key}: only power_on / power_off can be guarded")
        if b.guarded and b.discrete:
            out.append(f"{cs.name}: {key} is either discrete or guarded, not both")
        if not 0 <= b.command <= 0xFF:
            out.append(f"{cs.name}: {key} command {b.command} out of range")
    if any(b.guarded for b in cs.buttons.values()) and not all(
            k in cs.buttons and cs.buttons[k].guarded for k in ("power_on", "power_off")):
        out.append(f"{cs.name}: guarded power needs both power_on and power_off guarded")
    return out
