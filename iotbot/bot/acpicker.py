"""AC state picker: one message, [-] temp [+], fan row, vane rows, Powerful, Done.

The whole state rides in the callback data (5 chars, e.g. "223a0" = 22C, fan 3,
vane auto, powerful off), so a tap needs no server memory and survives restarts.
Callback: `ap:<owner>:<state>:<op>`; the owner (e.g. a wizard draft id) decides
what Done means.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup

from iotbot.ac.state import FANS, TEMP_MAX, TEMP_MIN, VANES, AcState, AcStateError
from iotbot.bot.callbacks import encode

NS = "ap"
_FAN = {"auto": "a", 1: "1", 2: "2", 3: "3", 4: "4", "quiet": "q"}
_VANE = {"auto": "a", 1: "1", 2: "2", 3: "3", 4: "4", 5: "5", "swing": "s"}
_FAN_R = {v: k for k, v in _FAN.items()}
_VANE_R = {v: k for k, v in _VANE.items()}


def encode_state(st: AcState) -> str:
    return f"{st.temp:02d}{_FAN[st.fan]}{_VANE[st.vane]}{int(st.powerful)}"


def decode_state(code: str) -> AcState | None:
    """The picker state in `code` (always power on, cool), or None if it is not one."""
    if len(code) != 5 or not code[:2].isdigit() or code[4] not in "01":
        return None
    fan, vane = _FAN_R.get(code[2]), _VANE_R.get(code[3])
    if fan is None or vane is None:
        return None
    try:
        return AcState(power=True, temp=int(code[:2]), fan=fan, vane=vane, powerful=code[4] == "1")
    except AcStateError:
        return None


def apply_op(st: AcState, op: str, preset: AcState) -> AcState:
    """One tap: 't+' / 't-' temp, 'f<x>' fan, 'v<x>' vane, 'p' powerful, 'r' reset."""
    if op == "t+":
        return st.with_changes(temp=min(TEMP_MAX, st.temp + 1))
    if op == "t-":
        return st.with_changes(temp=max(TEMP_MIN, st.temp - 1))
    if op.startswith("f") and op[1:] in _FAN_R:
        return st.with_changes(fan=_FAN_R[op[1:]])
    if op.startswith("v") and op[1:] in _VANE_R:
        return st.with_changes(vane=_VANE_R[op[1:]])
    if op == "p":
        return st.with_changes(powerful=not st.powerful)
    if op == "r":
        return preset.with_changes(power=True, powerful=False)
    return st


def picker_text(device: str, st: AcState) -> str:
    """Plain text; the caller escapes it (device ids are restricted to [A-Za-z0-9_-])."""
    return f"{device}: {st.describe()[3:]}\nTap to change, then Done."


def picker_keyboard(owner: str, st: AcState, done_label: str = "Done") -> InlineKeyboardMarkup:
    code = encode_state(st)

    def btn(label: str, op: str) -> Btn:
        return Btn(label, callback_data=encode(NS, owner, code, op))

    def mark(label: str, on: bool) -> str:
        return f"[{label}]" if on else label

    rows = [
        [btn("-", "t-"), btn(f"{st.temp}C", "noop"), btn("+", "t+")],
        [btn(mark("fan auto" if f == "auto" else str(f), st.fan == f), "f" + _FAN[f]) for f in FANS],
        [btn(mark("vane auto" if v == "auto" else str(v), st.vane == v), "v" + _VANE[v]) for v in VANES[:4]],
        [btn(mark(str(v), st.vane == v), "v" + _VANE[v]) for v in VANES[4:]],
        [btn(f"Powerful: {'on' if st.powerful else 'off'}", "p")],
        [btn(done_label, "done"), btn("Reset", "r"), btn("Cancel", "x")],
    ]
    return InlineKeyboardMarkup(rows)
