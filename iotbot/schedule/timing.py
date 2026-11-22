"""When schedules fire: occurrences, skips, pauses. Pure functions, no I/O.

An occurrence is one planned fire time, in the configured timezone. Its key
(`<id>@<local ISO time>`) is stored as `last_fired.occurrence`, so a restart or
a clock jump can never fire the same occurrence twice.

Skip dates are the local date of the fire time: skipping "Friday night 01:00"
stores Saturday's date.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from iotbot.schedule.model import DAYS, Once, Schedule

HORIZON_DAYS = 400   # weekly search limit; any non-empty day set repeats within 7


def local_time(day: date, hhmm: str, tz: ZoneInfo) -> datetime:
    """`hhmm` on `day` in `tz`, normalised (a time skipped by a DST jump moves forward)."""
    h, m = map(int, hhmm.split(":"))
    naive = datetime.combine(day, time(h, m))
    return datetime.fromtimestamp(naive.replace(tzinfo=tz).timestamp(), tz)


def occurrences(s: Schedule, after: datetime, tz: ZoneInfo) -> Iterator[datetime]:
    """Planned fire times strictly after `after`, earliest first, ignoring skip/pause/enabled."""
    if isinstance(s.when, Once):
        if s.when.at > after:
            yield s.when.at.astimezone(tz)
        return
    days = {DAYS.index(d) for d in s.when.days}
    start = after.astimezone(tz).date() - timedelta(days=1)   # a DST shift can move a time across midnight
    for n in range(HORIZON_DAYS):
        day = start + timedelta(days=n)
        if day.weekday() in days:
            at = local_time(day, s.when.time, tz)
            if at > after:
                yield at


def blocked(s: Schedule, at: datetime, tz: ZoneInfo) -> str | None:
    """Why the occurrence at `at` will not fire: 'disabled', 'paused', 'skipped', or None."""
    if not s.enabled:
        return "disabled"
    if s.paused_until is not None and at < s.paused_until:
        return "paused"
    if at.astimezone(tz).date() in s.skip_dates:
        return "skipped"
    return None


def next_runs(s: Schedule, after: datetime, tz: ZoneInfo, n: int = 1) -> list[datetime]:
    """The next `n` occurrences that will actually fire. A paused schedule is searched
    from its resume time, so a long pause neither looks like "never" nor costs a walk
    through every paused day."""
    out = []
    if not s.enabled:
        return out
    if s.paused_until is not None and s.paused_until > after:
        after = s.paused_until - timedelta(microseconds=1)   # a run exactly at the resume time fires
    for at in occurrences(s, after, tz):
        if blocked(s, at, tz) is None:
            out.append(at)
            if len(out) == n:
                break
    return out


def due(s: Schedule, start: datetime, end: datetime, tz: ZoneInfo) -> list[datetime]:
    """Occurrences in (start, end], blocked ones included (the caller logs why they did not fire)."""
    out = []
    for at in occurrences(s, start, tz):
        if at > end:
            break
        out.append(at)
    return out


def occurrence_key(s: Schedule, at: datetime, tz: ZoneInfo) -> str:
    return f"{s.id}@{at.astimezone(tz).isoformat()}"


def key_time(key: str, sid: str | None = None) -> datetime | None:
    """The fire time inside an occurrence key, or None if it is not one (or, with
    `sid`, belongs to another schedule)."""
    owner, _, iso = key.partition("@")
    if sid is not None and owner != sid:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def live_skip_dates(s: Schedule, today: date) -> tuple[date, ...]:
    """Skip dates still ahead (today included); past ones can be pruned."""
    return tuple(d for d in s.skip_dates if d >= today)


def fire_date_for_skip(s: Schedule, after: datetime, tz: ZoneInfo) -> date | None:
    """The date to store for "Skip next": the local date of the next run that would fire."""
    runs = next_runs(s, after, tz)
    return runs[0].astimezone(tz).date() if runs else None


def already_fired(s: Schedule, at: datetime) -> bool:
    """True if `at` is at or before the last handled occurrence. Compares times, not
    key strings, so a TZ_NAME change cannot make an old run look new."""
    if s.last_fired is None:
        return False
    t = key_time(s.last_fired.occurrence, s.id)
    return t is not None and at <= t


def passed_today(s: Schedule, now: datetime, tz: ZoneInfo) -> datetime | None:
    """Today's run time if the schedule runs today and that time has already passed
    (for "22:30 already passed tonight: [Run now] [From tomorrow]")."""
    if isinstance(s.when, Once):
        return None
    today = now.astimezone(tz).date()
    if DAYS[today.weekday()] not in s.when.days:
        return None
    at = local_time(today, s.when.time, tz)
    return at if at <= now else None
