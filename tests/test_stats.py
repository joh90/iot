"""/stats (Phase 6.4): percentiles, failures, slowest remotes, LLM health."""

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from iotbot.bot import handlers as hd
from iotbot.bot import stats
from iotbot.devices.hub import HubError
from iotbot.result import Actor
from iotbot.store import JsonlLog
from tests.test_ac_service import ALICE, make_ctx
from tests.test_bot import ME, make_context, make_update, replied

SGT = ZoneInfo("Asia/Singapore")


def turn(surface="button", total_ms=100, outcome="ok", **kw):
    return {"surface": surface, "total_ms": total_ms, "outcome": outcome, "allowed": True, **kw}


def event(device="bed_ac", ms=100, ok=True, **kw):
    return {"device": device, "ms": ms, "ok": ok, "surface": "button", "user_id": ME, **kw}


def names(uid):
    return {ME: "Alice"}.get(uid, str(uid))


def text(lines):
    return "\n".join(lines)


def test_nearest_rank_percentile():
    assert stats.pct(list(range(1, 11)), 50) == 5
    assert stats.pct(list(range(1, 11)), 95) == 10
    assert stats.pct([7], 95) == 7


def test_speed_by_surface_with_target_share(tmp_path):
    reg = make_ctx(tmp_path).registry
    ts = [turn(total_ms=v) for v in (100, 120, 150, 200, 400)] + [turn("slash", 90)]
    out = text(stats.summary(ts, [], reg, 7, names))
    assert "- buttons: 150 / 400 ms (5x), 80% under 300 ms" in out
    assert "- commands: 1x, too few to tell, 100% under 300 ms" in out


def test_denied_and_dropped_turns_are_not_speed(tmp_path):
    reg = make_ctx(tmp_path).registry
    ts = [turn(total_ms=5000, outcome="denied", allowed=False, user_id=99),
          turn(total_ms=5000, outcome="dropped_offline")] + [turn(total_ms=100)] * 5
    out = text(stats.summary(ts, [], reg, 7, names))
    assert "- buttons: 100 / 100 ms (5x), 100% under 300 ms" in out
    assert "- strangers: 1 message(s) from 1 id(s)" in out and "- dropped while the bot was offline: 1" in out


def test_message_delay_and_send_time(tmp_path):
    reg = make_ctx(tmp_path).registry
    ts = [turn("slash", lag_s=v) for v in (0, 1, 1, 2, 9)]
    out = text(stats.summary(ts, [event(ms=v) for v in (50, 60, 70, 80, 90)], reg, 1, names))
    assert "- message delay (Telegram to bot): 1 / 9 s (5x)" in out
    assert "- IR / plug send: 70 / 90 ms (5x)" in out and "last 1 day</b>" in out


def test_slowest_senders_group_by_rm_and_need_samples(tmp_path):
    reg = make_ctx(tmp_path).registry
    evs = [event("bed_ac", 300)] * 5 + [event("office_ac", 100)] * 3 + [event("old_ac", 200)] * 2
    out = text(stats.summary([], evs, reg, 7, names))
    assert "- bedroom remote: 300 ms (5x)" in out
    assert "- office remote: 150 ms" not in out          # office has 5 sends over two devices
    assert "- office remote: 200 ms (5x)" in out
    out = text(stats.summary([], [event()] * 2, reg, 7, names))
    assert "not enough sends yet" in out


def test_failures_by_device_and_who(tmp_path):
    reg = make_ctx(tmp_path).registry
    evs = [event(ok=False, error="no reply"), event(ok=False, error="timeout", maybe_delivered=True),
           event(ok=False, error="timeout", surface="scheduler", user_id=0), event("office_ac", ok=False, error="x")]
    out = text(stats.summary([], evs, reg, 7, names))
    assert "Failed sends</b> (4)" in out
    assert "- bed_ac: 3 (2 Alice, 1 schedule), 1 may have arrived; last: timeout" in out
    assert out.index("bed_ac: 3") < out.index("office_ac: 1")
    assert "- none" not in out.split("Failed sends")[1].split("Other")[0]


def test_empty_logs(tmp_path):
    out = text(stats.summary([], [], make_ctx(tmp_path).registry, 7, names))
    assert "- nothing yet" in out and "Failed sends</b> (0)\n- none" in out


def test_read_since_spans_months_and_cuts_old(tmp_path):
    log = JsonlLog(tmp_path, "turns")
    now = datetime(2026, 11, 2, 12, 0, tzinfo=SGT)
    for when in (datetime(2026, 9, 30, tzinfo=SGT), datetime(2026, 10, 20, tzinfo=SGT), now - timedelta(hours=1)):
        log.append({"at": when.isoformat()}, when=when)
    got = stats.read_since(log, now - timedelta(days=30), now)
    assert [r["at"][:10] for r in got] == ["2026-10-20", "2026-11-02"]


async def test_llm_up_down_and_not_ready():
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"data": [{"id": "qwen36-35b-a3b"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert (await stats.llm_health("http://llm", c)).startswith("up, qwen36-35b-a3b, ")

    def refuse(request):
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as c:
        assert await stats.llm_health("http://llm", c) == "down (ConnectError)"

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as c:
        assert (await stats.llm_health("http://llm", c)).startswith("not ready (HTTP 503")

    def no_models(request):
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(200, text="not json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_models)) as c:
        assert "model unknown" in await stats.llm_health("http://llm", c)


async def test_llm_down_on_a_closed_port():
    assert (await stats.llm_health("http://127.0.0.1:9")).startswith("down (")


# ---- the command ----------------------------------------------------------------------

async def test_stats_command_end_to_end(tmp_path, monkeypatch):
    async def fake_llm(url):
        return "up, test-model, 5 ms"

    monkeypatch.setattr(stats, "llm_health", fake_llm)
    ctx = make_ctx(tmp_path)
    for _ in range(5):
        async with ctx.tracker.turn(surface="slash", allowed=True, user_id=ME):
            await ctx.devices.run("bed_ac", "power_on", ALICE)
    ctx.fail = HubError("no reply")
    await ctx.devices.run("bed_ac", "power_off", Actor(0, "schedule x", "scheduler"))
    u = make_update(ME)
    await hd.cmd_stats(u, make_context(ctx))
    out = replied(u)
    assert "Stats, last 7 days</b> (5 turns)" in out and "- commands: " in out
    assert "- bed_ac: 1 (1 schedule)" in out
    assert "- up, test-model, 5 ms (http://127.0.0.1:8001)" in out


async def test_stats_rejects_bad_days(tmp_path):
    ctx = make_ctx(tmp_path)
    for args in (["0"], ["366"], ["week"], ["7", "8"], ["\u00b2"], ["\u0663"]):
        u = make_update(ME)
        await hd.cmd_stats(u, make_context(ctx, args))
        assert replied(u).startswith("Usage: /stats [days]")


async def test_stats_is_for_approved_users_only(tmp_path):
    u = make_update(99)
    await hd.cmd_stats(u, make_context(make_ctx(tmp_path)))
    assert "not approved" in replied(u)


# ---- review follow-ups ------------------------------------------------------------------

def test_read_since_year_rollover_bad_lines_and_keys(tmp_path):
    log = JsonlLog(tmp_path, "turns")
    now = datetime(2027, 1, 3, tzinfo=SGT)
    log.append({"raw": "secret", "total_ms": 5}, when=datetime(2026, 12, 30, tzinfo=SGT))
    with open(tmp_path / "turns-2026-12.jsonl", "a") as f:
        f.write('{"no_ts": 1}\n["not", "a", "dict"]\n{"ts": "2026-12-31T00:00:00"}\n')
    log.append({"raw": "x", "total_ms": 7}, when=datetime(2027, 1, 2, tzinfo=SGT))
    got = stats.read_since(log, now - timedelta(days=7), now, ("total_ms",))
    assert got == [{"total_ms": 5}, {"total_ms": 7}]


def test_names_and_errors_are_escaped(tmp_path):
    reg = make_ctx(tmp_path).registry
    out = text(stats.summary([], [event(ok=False, error="<b>&")], reg, 7, lambda uid: "<i>Bob"))
    assert "&lt;i&gt;Bob" in out and "&lt;b&gt;&amp;" in out and "<i>" not in out


def test_odd_old_lines_do_not_break_stats(tmp_path):
    reg = make_ctx(tmp_path).registry
    ts = [turn("slash", lag_s=v) for v in (True, -3600, None, "x", 2)]
    evs = [{"device": "bed_ac", "ms": None}, {"device": ["x"], "ms": 5, "ok": True},
           event(ok=False, user_id=None, error="e")]
    out = text(stats.summary(ts, evs, reg, 7, names))
    assert "message delay (Telegram to bot): 1x" in out          # only the 2 s line counts
    assert "- bed_ac: 1 (1 unknown)" in out


async def test_llm_never_raises_or_hangs():
    assert (await stats.llm_health("http://127.0.0.1:99999")).startswith("down (")

    def dict_models(request):
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(200, json={"data": {"id": "x"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(dict_models)) as c:
        assert "model unknown" in await stats.llm_health("http://llm", c)

    async def slow(request):
        await asyncio.sleep(10)

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as c:
        started = asyncio.get_running_loop().time()
        assert await stats.llm_health("http://llm", c) == "down (no answer in 2 s)"
        assert asyncio.get_running_loop().time() - started < 3
