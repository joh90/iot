from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from iotbot.schedule import model as sm
from iotbot.schedule import timing as tm
from tests.test_schedule_model import timer, weekly

SGT = ZoneInfo("Asia/Singapore")
# Sat 2026-10-10 12:00
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=SGT)


def at(day, hhmm, tz=SGT):
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 10, day, h, m, tzinfo=tz)


def test_weekly_next_runs():
    s = weekly(when=sm.make_weekly("23:00", ["sun", "mon", "tue", "wed", "thu"]))
    assert tm.next_runs(s, NOW, SGT, 3) == [at(11, "23:00"), at(12, "23:00"), at(13, "23:00")]


def test_weekly_today_later_and_exactly_now():
    s = weekly(when=sm.make_weekly("12:00", ["sat"]))
    assert tm.next_runs(s, NOW, SGT) == [at(17, "12:00")]          # strictly after
    assert tm.next_runs(s, NOW - timedelta(seconds=1), SGT) == [NOW]


def test_after_midnight_belongs_to_next_day():
    s = weekly(when=sm.make_weekly("01:00", ["sat"]))
    assert tm.next_runs(s, at(9, "23:00"), SGT) == [at(10, "01:00")]  # Fri night -> Sat 01:00


def test_once():
    t = timer()
    assert tm.next_runs(t, NOW, SGT) == [datetime(2026, 10, 11, 1, 30, tzinfo=SGT)]
    assert tm.next_runs(t, t.when.at, SGT) == []
    assert tm.due(t, NOW, t.when.at, SGT) == [t.when.at]


def test_skip_pause_disabled():
    s = weekly(when=sm.make_weekly("23:00", ["sat", "sun"]))
    assert tm.blocked(s, at(10, "23:00"), SGT) is None
    skipped = weekly(when=s.when, skip_dates=(date(2026, 10, 10),))
    assert tm.blocked(skipped, at(10, "23:00"), SGT) == "skipped"
    assert tm.next_runs(skipped, NOW, SGT) == [at(11, "23:00")]
    paused = weekly(when=s.when, paused_until=at(17, "00:00"))
    assert tm.blocked(paused, at(11, "23:00"), SGT) == "paused"
    assert tm.next_runs(paused, NOW, SGT) == [at(17, "23:00")]
    off = weekly(when=s.when, enabled=False)
    assert tm.blocked(off, at(10, "23:00"), SGT) == "disabled"
    assert tm.next_runs(off, NOW, SGT) == []


def test_skip_date_is_fire_date():
    s = weekly(when=sm.make_weekly("01:00", ["sat"]))
    # "Skip next" on Friday evening skips the run at Sat 01:00, stored as Saturday
    assert tm.fire_date_for_skip(s, at(9, "20:00"), SGT) == date(2026, 10, 10)
    assert tm.fire_date_for_skip(weekly(enabled=False), NOW, SGT) is None


def test_due_window_includes_blocked():
    s = weekly(when=sm.make_weekly("23:00", ["sat", "sun"]), skip_dates=(date(2026, 10, 10),))
    assert tm.due(s, NOW, at(12, "00:00"), SGT) == [at(10, "23:00"), at(11, "23:00")]
    assert tm.due(s, at(10, "23:00"), at(11, "22:59"), SGT) == []     # start is exclusive


def test_occurrence_key_round_trip():
    s = weekly()
    k = tm.occurrence_key(s, at(11, "23:00").astimezone(ZoneInfo("UTC")), SGT)
    assert k == "s7f3a@2026-10-11T23:00:00+08:00"
    assert tm.key_time(k) == at(11, "23:00")
    assert tm.key_time("junk") is None and tm.key_time("s1@2026-10-11T23:00") is None


def test_live_skip_dates():
    s = weekly(skip_dates=(date(2026, 10, 9), date(2026, 10, 10), date(2026, 10, 20)))
    assert tm.live_skip_dates(s, date(2026, 10, 10)) == (date(2026, 10, 10), date(2026, 10, 20))


def test_dst_zone_is_handled():
    # TZ_NAME is configurable; Singapore has no DST but other zones must not break
    lon = ZoneInfo("Europe/London")
    s = weekly(when=sm.make_weekly("01:30", ["sun"]))
    spring = tm.next_runs(s, datetime(2026, 3, 28, 12, 0, tzinfo=lon), lon)[0]
    assert spring == datetime(2026, 3, 29, 2, 30, tzinfo=lon)          # 01:30 does not exist
    autumn = tm.next_runs(s, datetime(2026, 10, 24, 12, 0, tzinfo=lon), lon)
    assert autumn[0].utcoffset() == timedelta(hours=1)                  # first of the two 01:30s
    runs = tm.next_runs(s, datetime(2026, 10, 24, 12, 0, tzinfo=lon), lon, 3)
    assert len(set(runs)) == 3 and runs == sorted(runs)


def test_long_pause_resumes_and_is_cheap():
    s = weekly(when=sm.make_weekly("23:00", list(sm.DAYS)), paused_until=datetime(2027, 12, 1, 23, 0, tzinfo=SGT))
    assert tm.next_runs(s, NOW, SGT) == [datetime(2027, 12, 1, 23, 0, tzinfo=SGT)]   # run at resume time fires
    assert tm.fire_date_for_skip(s, NOW, SGT) == date(2027, 12, 1)
    import time
    t0 = time.perf_counter()
    for _ in range(50):
        tm.next_runs(s, NOW, SGT)
    assert time.perf_counter() - t0 < 0.05


def test_paused_until_equal_to_occurrence_fires():
    s = weekly(when=sm.make_weekly("23:00", ["sat"]), paused_until=at(10, "23:00"))
    assert tm.blocked(s, at(10, "23:00"), SGT) is None
    assert tm.next_runs(s, NOW, SGT) == [at(10, "23:00")]


def test_timer_in_other_offset():
    utc = ZoneInfo("UTC")
    t = timer(when=sm.Once(datetime(2026, 10, 11, 1, 30, tzinfo=utc)))
    (run,) = tm.next_runs(t, NOW, SGT)
    assert run.utcoffset() == timedelta(hours=8) and run == datetime(2026, 10, 11, 1, 30, tzinfo=utc)
    assert tm.occurrence_key(t, run, SGT) == "t91c2@2026-10-11T09:30:00+08:00"


def test_due_across_fall_back_fires_once():
    lon = ZoneInfo("Europe/London")
    s = weekly(when=sm.make_weekly("01:30", ["sun"]))
    start = datetime(2026, 10, 25, 0, 0, tzinfo=lon)
    hits = []
    for minute in range(0, 240, 10):          # 10-minute sliding windows across the repeated hour
        a = start + timedelta(minutes=minute)
        hits += tm.due(s, a, a + timedelta(minutes=10), lon)
    assert len(hits) == 1


def test_midnight_switch_zone():
    stgo = ZoneInfo("America/Santiago")
    s = weekly(when=sm.make_weekly("00:00", list(sm.DAYS)))
    runs = tm.next_runs(s, datetime(2026, 9, 1, 12, 0, tzinfo=stgo), stgo, 30)
    assert len({r.date() for r in runs}) == 30 and runs == sorted(runs)


def test_midnight_on_a_weekday():
    s = weekly(when=sm.make_weekly("00:00", ["mon"]))
    assert tm.next_runs(s, NOW, SGT) == [datetime(2026, 10, 12, 0, 0, tzinfo=SGT)]


def test_due_returns_blocked_runs_of_disabled():
    s = weekly(when=sm.make_weekly("23:00", ["sat"]), enabled=False)
    assert tm.due(s, NOW, at(11, "00:00"), SGT) == [at(10, "23:00")]
    assert tm.next_runs(s, NOW, SGT) == []


def test_already_fired_and_key_owner():
    key = "s7f3a@2026-10-10T23:00:00+08:00"
    s = weekly(last_fired=sm.LastFired(NOW, key, "ok"))
    assert tm.already_fired(s, at(10, "23:00")) and not tm.already_fired(s, at(11, "23:00"))
    assert tm.key_time(key, "s0000") is None
    other = weekly(last_fired=sm.LastFired(NOW, "s0000@2026-10-10T23:00:00+08:00", "ok"))
    assert not tm.already_fired(other, at(10, "22:00"))


def test_passed_today():
    s = weekly(when=sm.make_weekly("11:00", ["sat"]))
    assert tm.passed_today(s, NOW, SGT) == at(10, "11:00")
    assert tm.passed_today(weekly(when=sm.make_weekly("13:00", ["sat"])), NOW, SGT) is None
    assert tm.passed_today(weekly(when=sm.make_weekly("11:00", ["sun"])), NOW, SGT) is None
    assert tm.passed_today(timer(), NOW, SGT) is None
