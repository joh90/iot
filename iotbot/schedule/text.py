"""Short human text for schedules: shared by messages, buttons and later the LLM."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from iotbot.schedule import timing as tm
from iotbot.schedule.model import DAYS, Adjust, Capture, Off, On, Once, Schedule, Weekly

NIGHT_END = 5     # a run before 05:00 belongs to the previous night ("Fri night (Sat 01:00)")


def describe_action(s: Schedule) -> str:
    a = s.action
    if isinstance(a, On):
        st = a.state
        text = f"on, {st.mode} {st.temp}C fan {st.fan} vane {st.vane}"
        return text + (" powerful" if st.powerful else "")
    if isinstance(a, Off):
        return "off"
    if isinstance(a, Adjust):
        return "set " + ", ".join(f"{k} {v}" for k, v in a.changes)
    if isinstance(a, Capture):
        return a.key.replace("_", " ")
    return "?"


def describe_days(days: tuple[str, ...]) -> str:
    if len(days) == 7:
        return "every day"
    if days == ("mon", "tue", "wed", "thu", "fri"):
        return "weekdays"
    if days == ("sat", "sun"):
        return "weekends"
    idx = [DAYS.index(d) for d in days]
    if len(idx) >= 3:
        # One run of consecutive days, possibly wrapping the week (Sun-Thu)
        for start in idx:
            run = [(start + k) % 7 for k in range(len(idx))]
            if sorted(run) == idx:
                return f"{DAYS[run[0]].title()}-{DAYS[run[-1]].title()}"
    return ", ".join(d.title() for d in days)


def fmt_day(at: datetime, now: datetime) -> str:
    """'today', 'tomorrow' or 'Mon 12 Oct'."""
    d, today = at.date(), now.date()
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return f"{at:%a} {at.day} {at:%b}"


def fmt_at(at: datetime, now: datetime, tz: ZoneInfo) -> str:
    at, now = at.astimezone(tz), now.astimezone(tz)
    return f"{fmt_day(at, now)} {at:%H:%M}"


def describe_when(s: Schedule, now: datetime, tz: ZoneInfo) -> str:
    w = s.when
    if isinstance(w, Weekly):
        return f"{w.time} {describe_days(w.days)}"
    assert isinstance(w, Once)
    return f"once, {fmt_at(w.at, now, tz)}"


def night_of(fire_date: date, hhmm: str) -> str:
    """'Fri night (Sat 01:00)' for small hours, else 'Sat 10 Oct'."""
    if int(hhmm[:2]) < NIGHT_END:
        prev = fire_date - timedelta(days=1)
        return f"{prev:%a} {prev.day} {prev:%b} night ({fire_date:%a} {hhmm})"
    return f"{fire_date:%a} {fire_date.day} {fire_date:%b}"


def name(s: Schedule) -> str:
    return s.label or s.id


def describe(s: Schedule, now: datetime, tz: ZoneInfo) -> str:
    """'Bedtime: bed_ac on, cool 22C fan 3 vane auto, 23:00 Sun-Thu'."""
    head = f"{s.label}: " if s.label else ""
    return f"{head}{s.device} {describe_action(s)}, {describe_when(s, now, tz)}"


def is_paused(s: Schedule, now: datetime) -> bool:
    return not s.enabled or (s.paused_until is not None and s.paused_until > now)


def state_line(s: Schedule, now: datetime, tz: ZoneInfo) -> str:
    """'Paused until you resume it.' / 'Skipping Tue 21 Oct.' / '' -- the state the buttons act on."""
    if not s.enabled:
        return "Paused until you resume it."
    if s.paused_until is not None and s.paused_until > now:
        return f"Paused until {fmt_at(s.paused_until, now, tz)}."
    # By fire time, not date: a skipped 01:00 run is over at 02:00 the same day
    skips = [d for d in s.skip_dates if isinstance(s.when, Weekly) and tm.local_time(d, s.when.time, tz) > now]
    if skips and isinstance(s.when, Weekly):
        return "Skipping " + ", ".join(night_of(d, s.when.time) for d in skips) + "."
    return ""


def toggles(s: Schedule, now: datetime, tz: ZoneInfo) -> list[tuple[str, str, tuple[str, ...]]]:
    """Buttons that follow the schedule's state, as (label, op, extra args): [Skip next] or
    [Don't skip] (the first skipped run), then [Pause] or [Resume]. A paused schedule shows
    only [Resume]; a timer only [Resume] when disabled."""
    if s.is_timer:
        return [("Resume", "resume", ())] if not s.enabled else []
    if is_paused(s, now):
        return [("Resume", "resume", ())]
    # [Don't skip] only when the run that would come next is the skipped one; a skip further
    # ahead (e.g. Fri, set in chat) must not hide [Skip next] for tonight
    runs = tm.next_runs(replace(s, skip_dates=()), now, tz, 1)
    nxt = runs[0].astimezone(tz).date() if runs else None
    first = (("Don't skip", "unskip", (f"{nxt:%Y%m%d}",)) if nxt in s.skip_dates else ("Skip next", "skip", ()))
    return [first, ("Pause", "pause", ())]


def parse_day(text: str) -> date | None:
    """'20261021' (a button's skip date) -> date."""
    try:
        return datetime.strptime(text, "%Y%m%d").date() if len(text) == 8 and text.isdecimal() else None
    except ValueError:
        return None
