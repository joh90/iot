"""Phase 4.8: NEA feeds, AQI as aqicn.org shows it, bands, /outside (recorded replies, never online)."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from iotbot.bot import handlers as hd
from iotbot.llm.tools import parse_args, schema_problems
from iotbot.llm.world import OutsideTools
from iotbot.outside import air
from iotbot.outside.nea import FRESH_S, STALE_MAX_S, NeaClient, NeaError
from iotbot.outside.weather import AREAS, Outside
from iotbot.result import Actor
from tests.test_ac_service import make_ctx
from tests.test_bot import ME, make_context, make_update, replied

FIX = Path(__file__).parent / "fixtures" / "nea"
SGT = ZoneInfo("Asia/Singapore")
HOME = "Bedok"
# Recorded 2026-10-15 around 02:00 SGT, during haze; lightning from 2026-10-14 23:10-23:58
AT = datetime(2026, 10, 15, 2, 20, tzinfo=SGT).timestamp()


class Clock:
    def __init__(self, t=AT):
        self.t = t

    def __call__(self):
        return self.t


class Nea:
    """Serves the recorded replies; `down` makes every call fail; counts calls per feed."""

    def __init__(self):
        self.down = False
        self.status = 200
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        feed = request.url.path.rsplit("/", 1)[-1]
        if feed == "weather":
            feed = request.url.params["api"]
        self.calls.append(feed)
        if self.down:
            raise httpx.ConnectError("NEA down")
        if self.status != 200:
            return httpx.Response(self.status, json={"code": 429, "errorMsg": "Too many requests"})
        return httpx.Response(200, content=(FIX / f"{feed}.json").read_bytes())


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch, request):
    """Tests run many feeds back to back; only the throttle test keeps the real budget."""
    if request.node.name != "test_throttle_waits_for_a_slot":
        from iotbot.outside import nea as mod
        monkeypatch.setattr(mod, "MAX_CALLS", 1000)


def outside(clock=None, nea=None):
    nea = nea or Nea()
    clock = clock or Clock()
    client = NeaClient(httpx.AsyncClient(transport=httpx.MockTransport(nea)), clock=clock)
    return Outside(client, "Asia/Singapore", home=HOME, home_regions=("east", "central")), nea, clock


# ---- AQI and bands ---------------------------------------------------------------------

@pytest.mark.parametrize("conc,aqi", [(0, 0), (12.0, 50), (12.05, 50), (12.1, 51), (35.4, 100), (55.5, 151),
                                       (93, 170), (85, 166), (150.4, 200), (500.4, 500), (900, 500)])
def test_aqi_pm25_uses_the_pre_2024_table(conc, aqi):
    assert air.aqi_pm25(conc) == aqi


def test_aqi_matches_aqicn_central_and_takes_the_highest_pollutant():
    # aqicn.org central, 2026-10-15 01:00: PM2.5 170, PM10 108, O3 29, SO2 4, CO 17 -> 170
    subs = {"pm10_sub_index": 108, "o3_sub_index": 29, "so2_sub_index": 4, "co_sub_index": 17}
    assert air.aqi(93, subs) == (170, "PM2.5")
    assert air.aqi(5, {"o3_sub_index": 120}) == (120, "O3")


@pytest.mark.parametrize("psi,band,advice", [(50, "good", "normal activities"), (100, "moderate", "normal activities"),
                                             (146, "unhealthy", "reduce prolonged or strenuous outdoor physical exertion"),
                                             (250, "very unhealthy", "avoid prolonged or strenuous outdoor physical exertion"),
                                             (301, "hazardous", "minimise outdoor activity")])
def test_psi_bands_and_nea_advice(psi, band, advice):
    assert (air.psi_band(psi), air.psi_advice(psi)) == (band, advice)


def test_other_bands():
    assert [air.aqi_band(v) for v in (50, 101, 151, 201, 301)] == [
        "good", "unhealthy for sensitive groups", "unhealthy", "very unhealthy", "hazardous"]
    assert [air.pm25_1h_band(v) for v in (55, 56, 151, 251)] == [
        "band I, normal", "band II, elevated", "band III, high", "band IV, very high"]
    assert [air.uv_band(v) for v in (2, 5, 7, 10, 11)] == ["low", "moderate", "high", "very high", "extreme"]


# ---- the summary and each part ---------------------------------------------------------

async def test_home_summary_is_five_calls_and_shows_east_and_central():
    o, nea, _ = outside()
    text = await o.summary()
    assert text.startswith("Outside, home (Bedok):\nNext 2h (2.00 am to 4.00 am): Cloudy (as of 02:06)")
    assert "Air, east (as of 02:00):\nAQI 168 unhealthy (aqicn-style, PM2.5 now)\nPSI 173 unhealthy (NEA 24h)\n" \
           "PM2.5 1h 89 (band II, elevated)" in text
    assert "Air, central (as of 02:00):\nAQI 166 unhealthy" in text and "PSI 186 unhealthy" in text
    assert text.endswith("NEA advice for healthy people: reduce prolonged or strenuous outdoor physical exertion.")
    assert "Rain: no rain in the last 5 min" in text and "Lightning" not in text       # not thundery
    assert len(nea.calls) == 5
    await o.summary()
    assert len(nea.calls) == 5                                                     # 10-min cache


async def test_named_area_uses_its_nearest_region_and_stations():
    o, _, _ = outside()
    r = await o.report("Jurong West", ["air", "now"])
    assert r["area"] == "Jurong West"
    assert r["lines"][0] == "Air, west (as of 02:00):" and "PSI 207 very unhealthy (NEA 24h)" in r["lines"]
    assert "avoid prolonged" in r["lines"][4]
    assert any(line.startswith("Now: ") and "Nanyang Avenue" in line for line in r["lines"])


async def test_every_part_reads_the_recorded_replies():
    o, _, _ = outside()
    r = await o.report(None, ["forecast_24h", "outlook_4day", "rain", "uv", "heat", "wind", "humidity"])
    text = "\n".join(r["lines"])
    assert "24h forecast, 0 AM 15 Oct to 0 AM 16 Oct (as of Wed 23:40): Cloudy, 25-34C, humidity 60-95%, " \
           "wind SSE 5-15 km/h" in text
    assert "Midnight to 6 am 15 Oct: Cloudy (east)" in text
    assert "Thursday: Afternoon thundery showers, 25-34C" in text
    assert "UV index: 0 (low), island-wide, at Wed 19:00 (NEA gives UV in daylight hours only)" in text
    assert "Heat stress: low (WBGT" in text and "Wind: " in text and " knots (" in text and "Humidity: " in text
    assert "NEA's reply was not understood" not in text and "cannot reach" not in text


async def test_lightning_counts_strikes_within_10km_in_the_last_30_min():
    clock = Clock(datetime(2026, 10, 14, 23, 59, tzinfo=SGT).timestamp())
    o, _, _ = outside(clock)
    line = (await o.report("Jurong West", ["lightning"]))["lines"][0]
    assert line.startswith("Lightning: 1 strike(s) within 10 km in the last 30 min (0 cloud-to-ground), nearest")
    line = (await o.report(None, ["lightning"]))["lines"][0]
    assert line == "Lightning: none within 10 km in the last 30 min"
    clock.t += 3600                                                                # an hour later: too old
    line = (await o.report("Jurong West", ["lightning"]))["lines"][0]
    assert line.startswith("Lightning: none")


async def test_thundery_forecast_adds_lightning_to_the_summary(monkeypatch):
    o, _, _ = outside()
    real = o._p_forecast_2h

    async def thundery(area):
        return [(await real(area))[0].replace("Cloudy", "Thundery Showers")]

    monkeypatch.setattr(o, "_p_forecast_2h", thundery)
    text = await o.summary()
    assert "\nLightning: none within 10 km" in text.split("Now:")[0]


# ---- NEA down, slow or rate limited ----------------------------------------------------

async def test_nea_down_uses_last_good_data_with_its_age_then_gives_up():
    o, nea, clock = outside()
    await o.summary()
    nea.down = True
    clock.t += FRESH_S + 60                                                        # cache expired, NEA down
    text = await o.summary()
    assert "PSI 173" in text and "NEA not answering now, 11 min old" in text
    clock.t += STALE_MAX_S                                                        # too old to show
    text = await o.summary()
    assert "PSI" not in text and "air: cannot reach NEA right now" in text


async def test_rate_limited_or_bad_replies_are_failures_not_crashes():
    nea = Nea()
    nea.status = 429
    o, _, _ = outside(nea=nea)
    text = await o.summary()
    assert "cannot reach NEA right now" in text
    client = NeaClient(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"code": 0, "data": {"items": []}}))), clock=Clock())
    o2 = Outside(client, "Asia/Singapore")
    assert "not understood" in (await o2.report(None, ["air"]))["lines"][0]
    with pytest.raises(NeaError):
        await NeaClient(httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"<html>")))).get("psi")


async def test_throttle_waits_for_a_slot(monkeypatch):
    from iotbot.outside import nea as mod
    o, nea, _ = outside()
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        o.nea._calls.clear()                                               # the window passes

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    await o.report(None, ["forecast_2h", "now", "air", "humidity"])         # 6 feeds: one wait
    assert len(nea.calls) == 6 and len(slept) == 1


# ---- the chat tool and /outside ------------------------------------------------------------

def test_tool_schema_and_args():
    o, _, _ = outside()
    tool = OutsideTools(o).tools(Actor(ME, "Alice", "llm"))[0]
    assert schema_problems(tool.parameters) == [] and not tool.write
    assert parse_args(tool, json.dumps({"area": "Bedok", "parts": ["air", "uv"]})) == {"area": "Bedok",
                                                                                       "parts": ["air", "uv"]}
    assert len(AREAS) == 47


async def test_tool_defaults_to_the_home_summary():
    o, _, _ = outside()
    out = await OutsideTools(o).tools(Actor(ME, "Alice", "llm"))[0].run({})
    assert out["area"] == "Bedok (home)" and out["lines"][0].startswith("Next 2h")
    assert "never add numbers" in out["note"]


def test_match_area():
    assert Outside.match_area("jurong  east") == ["Jurong East"]
    assert Outside.match_area("toa") == ["Toa Payoh"]
    assert Outside.match_area("jurong") == ["Jurong East", "Jurong Island", "Jurong West"]
    assert Outside.match_area("orchard") == []


async def test_outside_command(tmp_path):
    ctx = make_ctx(tmp_path, {"LLM_ENABLED": "off"})                         # works with the LLM off
    nea = Nea()
    ctx.outside.nea.http = httpx.AsyncClient(transport=httpx.MockTransport(nea))
    ctx.outside.nea._clock = ctx.outside._clock = Clock()
    u = make_update(ME)
    await hd.cmd_outside(u, make_context(ctx))
    assert "Outside, home (City):" in replied(u) and "Air, central (as of 02:00):\nAQI 166" in replied(u)
    u = make_update(ME)
    await hd.cmd_outside(u, make_context(ctx, ["toa", "payoh"]))
    assert "Outside, Toa Payoh:" in replied(u)
    u = make_update(ME)
    await hd.cmd_outside(u, make_context(ctx, ["<b>mars"]))
    assert "I don't know the area '&lt;b&gt;mars'" in replied(u)
    u = make_update(ME)
    await hd.cmd_outside(u, make_context(ctx, ["jurong"]))
    assert "'jurong' could be Jurong East, Jurong Island, Jurong West" in replied(u)
    u = make_update(ME)
    await hd.cmd_outside(u, make_context(ctx, ["West"]))
    assert "air for the west:" in replied(u) and "Air, west (as of 02:00):" in replied(u)


async def test_region_picks_the_air_readings():
    o, _, _ = outside()
    r = await o.report(None, ["air"], "central")
    assert r["region"] == "central" and r["lines"][0] == "Air, central (as of 02:00):" and len(r["lines"]) == 5
    r = await o.report(None, ["air"])                                     # the override does not stick
    assert "Air, east (as of 02:00):" in r["lines"]



def mutate(fn):
    """A Nea that changes one recorded reply before serving it."""
    class Changed(Nea):
        def __call__(self, request):
            r = super().__call__(request)
            if r.status_code != 200:
                return r
            body = json.loads(r.content)
            fn(request.url.path.rsplit("/", 1)[-1], body)
            return httpx.Response(200, json=body)
    return Changed()


async def test_odd_replies_cost_one_line_not_the_answer():
    def change(feed, body):
        if feed == "two-hr-forecast":
            body["data"]["items"][0]["forecasts"] = [f for f in body["data"]["items"][0]["forecasts"]
                                                     if f["area"] != HOME]
        if feed == "air-temperature":
            for x in body["data"]["readings"][0]["data"]:
                if x["stationId"] == "S107":                              # East Coast Park, nearest to home
                    x["value"] = None
        if feed == "pm25":
            body["data"]["items"][0]["readings"]["pm25_one_hourly"]["east"] = None
    o, _, _ = outside(nea=mutate(change))
    text = await o.summary()
    assert "forecast 2h: NEA's reply was not understood" in text
    assert text.count("Now: ") == 1 and "East Coast Park" not in text          # the next station instead
    assert "Air, east: NEA has no reading for this region right now" in text and "Air, central (as of" in text


async def test_failure_backoff_and_stale_psi_is_marked():
    o, nea, clock = outside()
    await o.report(None, ["air"])
    nea.down = True
    clock.t += FRESH_S + 60
    calls = len(nea.calls)
    r = await o.report(None, ["air"])
    assert "PSI 173 unhealthy (NEA 24h, as of 02:00, NEA not answering now, 11 min old)" in r["lines"]
    tried = len(nea.calls) - calls
    await o.report(None, ["air"])                                              # within the backoff
    assert tried == 2 and len(nea.calls) - calls == tried


async def test_stale_lightning_never_says_none():
    clock = Clock(datetime(2026, 10, 14, 23, 50, tzinfo=SGT).timestamp())
    o, nea, _ = outside(clock)
    await o.report("Jurong West", ["lightning"])
    nea.down = True
    clock.t += 360                                                             # 6 min later, NEA down
    line = (await o.report("Jurong West", ["lightning"]))["lines"][0]
    assert line == "Lightning: cannot tell, NEA not answering now (last data 6 min old)"


async def test_a_hanging_nea_is_cut_at_the_timeout(monkeypatch):
    import asyncio

    from iotbot.outside import nea as mod
    monkeypatch.setattr(mod, "TIMEOUT_S", 0.05)

    async def hang(request):
        await asyncio.sleep(5)

    client = NeaClient(httpx.AsyncClient(transport=httpx.MockTransport(hang)), clock=Clock())
    with pytest.raises(NeaError):
        await client.get("psi")


async def test_bad_reply_with_status_200():
    client = NeaClient(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"code": 17, "errorMsg": "nope", "data": None}))), clock=Clock())
    with pytest.raises(NeaError):
        await client.get("psi")


def test_home_area_and_regions_come_from_settings(tmp_path):
    from iotbot.config import ConfigError, load_settings
    s = load_settings(environ={"BOT_TOKEN": "1:x", "OUTSIDE_AREA": "toa  payoh", "OUTSIDE_REGIONS": "East, central"},
                      base_dir=tmp_path)
    assert (s.outside_area, s.outside_regions) == ("Toa Payoh", ("east", "central"))
    for env in ({"OUTSIDE_AREA": "Mars"}, {"OUTSIDE_REGIONS": "east,moon"}):
        with pytest.raises(ConfigError):
            load_settings(environ={"BOT_TOKEN": "1:x", **env}, base_dir=tmp_path)
    ctx = make_ctx(tmp_path, {"OUTSIDE_AREA": "Bedok"})
    assert ctx.outside.home == "Bedok" and ctx.outside.home_regions == ("central",)
