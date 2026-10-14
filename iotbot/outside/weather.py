"""Outside conditions for one place, worded in code (Phase 4.8).

Home is one of NEA's 47 forecast areas with its air regions (OUTSIDE_AREA, OUTSIDE_REGIONS in
.env, PLAN "Outside"); any other area can be named, and then its nearest region and stations
are used. Every part says when NEA measured it ("as of 01:00"), and
stale data (NEA not answering) says how old it is. The chat tool and /outside share this.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from iotbot.outside import air
from iotbot.outside.nea import Fetched, NeaClient, NeaError

logger = logging.getLogger(__name__)

REGIONS = ("north", "south", "east", "west", "central")
AREAS = (
    "Ang Mo Kio", "Bedok", "Bishan", "Boon Lay", "Bukit Batok", "Bukit Merah", "Bukit Panjang", "Bukit Timah",
    "Central Water Catchment", "Changi", "Choa Chu Kang", "City", "Clementi", "Geylang", "Hougang", "Jalan Bahar",
    "Jurong East", "Jurong Island", "Jurong West", "Kallang", "Lim Chu Kang", "Mandai", "Marine Parade", "Novena",
    "Pasir Ris", "Paya Lebar", "Pioneer", "Pulau Tekong", "Pulau Ubin", "Punggol", "Queenstown", "Seletar",
    "Sembawang", "Sengkang", "Sentosa", "Serangoon", "Southern Islands", "Sungei Kadut", "Tampines", "Tanglin",
    "Tengah", "Toa Payoh", "Tuas", "Western Islands", "Western Water Catchment", "Woodlands", "Yishun",
)
PARTS = ("now", "forecast_2h", "forecast_24h", "outlook_4day", "air", "rain", "lightning", "uv", "heat", "wind",
         "humidity")
# /outside and the default answer: 5 NEA calls (the throttle's 10 s budget); + lightning when thundery
SUMMARY = ("forecast_2h", "now", "air")
LIGHTNING_KM = 10.0
LIGHTNING_MIN = 30
# The region a request asked for ("air in central"), per request so concurrent asks never mix
_REGION: contextvars.ContextVar[str | None] = contextvars.ContextVar("region", default=None)
COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


class Unclear(Exception):
    """NEA's reply was not in the shape we know."""


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat = math.radians((a[0] + b[0]) / 2)
    return 111.2 * math.hypot(a[0] - b[0], (a[1] - b[1]) * math.cos(lat))


def _loc(d: dict[str, Any]) -> tuple[float, float]:
    return float(d["latitude"]), float(d["longitude"])


class Outside:
    def __init__(self, nea: NeaClient, tz: str, clock: Callable[[], float] | None = None,
                 home: str = "City", home_regions: tuple[str, ...] = ("central",)):
        self.nea = nea
        self.home = home
        self.home_regions = home_regions
        self.tz = ZoneInfo(tz)
        self._clock = clock or nea._clock

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), self.tz)

    # ---- public ------------------------------------------------------------------------

    async def report(self, area: str | None = None, parts: tuple[str, ...] | list[str] = SUMMARY,
                     region: str | None = None) -> dict[str, Any]:
        """{"area": ..., "lines": [...]} for the chat tool; parts that fail say so in their line.
        `region` (north/south/east/west/central) picks the air readings instead of the area's."""
        area = area if area in AREAS else self.home
        region = region if region in REGIONS else None
        token = _REGION.set(region)
        try:
            # In parallel: a slow NEA costs one timeout, not one per part
            done = await asyncio.gather(*(self._part(area, p) for p in parts))
            lines = [line for part in done for line in part]
        finally:
            _REGION.reset(token)
        out = {"area": area + (" (home)" if area == self.home else ""), "lines": lines}
        if region:
            out["region"] = region
        return out

    async def summary(self, area: str | None = None, region: str | None = None) -> str:
        """The /outside message: plain text, one screen."""
        r = await self.report(area, SUMMARY, region)
        if any("thunder" in line.lower() for line in r["lines"][:1]):
            r["lines"][1:1] = await self._part(area if area in AREAS else self.home, "lightning")
        if region in REGIONS:
            head = f"Outside, home ({self.home}), air for the {region}:"
        elif area in AREAS and area != self.home:
            head = f"Outside, {area}:"
        else:
            head = f"Outside, home ({self.home}):"
        return head + "\n" + "\n".join(r["lines"])

    @staticmethod
    def match_area(text: str) -> list[str]:
        """'/outside jurong east' -> ['Jurong East']; a unique prefix is enough ('toa' -> ['Toa Payoh']).
        Several hits = ambiguous ('jurong'); none = unknown."""
        t = " ".join(text.lower().split())
        if not t:
            return []
        exact = [a for a in AREAS if a.lower() == t]
        return exact or [a for a in AREAS if a.lower().startswith(t)]

    # ---- parts -------------------------------------------------------------------------

    async def _part(self, area: str, part: str) -> list[str]:
        fn = getattr(self, f"_p_{part}", None)
        if fn is None:
            return [f"{part}: not something I can look up"]
        try:
            return await fn(area)
        except NeaError as e:
            return [f"{part.replace('_', ' ')}: {e}"]
        except (Unclear, KeyError, IndexError, TypeError, ValueError, AttributeError):
            return [f"{part.replace('_', ' ')}: NEA's reply was not understood, so I cannot say"]
        except Exception:  # noqa: BLE001 -- one odd reply must not cost the whole answer
            logger.exception("Outside part %s failed", part)
            return [f"{part.replace('_', ' ')}: something went wrong reading NEA's reply"]

    def _as_of(self, f: Fetched, stamp: str | None) -> str:
        at = self._time(stamp)
        text = f"as of {at:%H:%M}" if at else "time unknown"
        if at and at.date() != self.now().date():
            text = f"as of {at:%a %H:%M}"
        if f.stale:
            text += f", NEA not answering now, {_age(f.age_s(self._clock()))} old"
        return text

    def _time(self, stamp: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(str(stamp)).astimezone(self.tz)
        except ValueError:
            return None

    async def _area_loc(self, area: str) -> tuple[Fetched, tuple[float, float]]:
        f = await self.nea.get("two-hr-forecast")
        for a in f.data["area_metadata"]:
            if a["name"] == area:
                return f, _loc(a["label_location"])
        raise Unclear(area)

    async def _p_forecast_2h(self, area: str) -> list[str]:
        f, _ = await self._area_loc(area)
        item = f.data["items"][0]
        text = next((x["forecast"] for x in item["forecasts"] if x["area"] == area), None)
        if not text:
            raise Unclear(area)
        period = item.get("valid_period", {}).get("text") or "next 2 hours"
        return [f"Next 2h ({period}): {text} ({self._as_of(f, item.get('update_timestamp'))})"]

    async def _stations(self, feed: str, loc: tuple[float, float],
                        need: set[str] | None = None) -> tuple[Fetched, str, float, float, str]:
        """Nearest station with a reading: (fetched, name, km, value, timestamp). Stations with no
        value (null) are skipped; `need` limits the choice to these station ids."""
        f = await self.nea.get(feed)
        d = f.data
        reading = d["readings"][0]
        values = {x["stationId"]: x["value"] for x in reading["data"] if isinstance(x.get("value"), (int, float))}
        best = min(((km(loc, _loc(s["location"])), s) for s in d["stations"]
                    if s["id"] in values and (need is None or s["id"] in need)), key=lambda t: t[0], default=None)
        if best is None:
            raise Unclear(feed)
        return f, best[1]["name"], best[0], float(values[best[1]["id"]]), reading["timestamp"]

    async def _ids(self, feed: str) -> set[str]:
        f = await self.nea.get(feed)
        return {x["stationId"] for x in f.data["readings"][0]["data"] if isinstance(x.get("value"), (int, float))}

    async def _p_now(self, area: str) -> list[str]:
        _, loc = await self._area_loc(area)
        (tf, tname, _, temp, tts), (rf, rname, rkm, rain, rts) = await asyncio.gather(
            self._stations("air-temperature", loc), self._stations("rainfall", loc))
        return [f"Now: {temp:.1f}C ({tname}, {self._as_of(tf, tts)})",
                f"Rain: {_rain(rain)} in the last 5 min ({rname}, {rkm:.0f} km, {self._as_of(rf, rts)})"]

    async def _p_humidity(self, area: str) -> list[str]:
        _, loc = await self._area_loc(area)
        f, name, _, hum, ts = await self._stations("relative-humidity", loc)
        return [f"Humidity: {hum:.0f}% ({name}, {self._as_of(f, ts)})"]

    async def _p_rain(self, area: str) -> list[str]:
        _, loc = await self._area_loc(area)
        f, name, dist, rain, ts = await self._stations("rainfall", loc)
        near = f.data["readings"][0]["data"]
        by_id = {s["id"]: s for s in f.data["stations"]}
        wet = sum(1 for x in near if isinstance(x.get("value"), (int, float)) and x["value"] > 0 and x["stationId"] in by_id
                  and km(loc, _loc(by_id[x["stationId"]]["location"])) <= 5)
        extra = f"; rain at {wet} station(s) within 5 km" if wet else "; no rain at stations within 5 km"
        return [f"Rain: {_rain(rain)} in the last 5 min ({name}, {dist:.0f} km{extra}, {self._as_of(f, ts)})"]

    async def _p_wind(self, area: str) -> list[str]:
        _, loc = await self._area_loc(area)
        both = (await self._ids("wind-speed")) & (await self._ids("wind-direction"))   # one station for both
        (sf, name, _, speed, ts), (_, _, _, deg, _) = await asyncio.gather(
            self._stations("wind-speed", loc, both), self._stations("wind-direction", loc, both))
        compass = COMPASS[round(deg / 22.5) % 16]
        return [f"Wind: {speed:.0f} knots ({speed * 1.852:.0f} km/h) from the {compass} ({name}, {self._as_of(sf, ts)})"]

    async def _p_heat(self, area: str) -> list[str]:
        _, loc = await self._area_loc(area)
        f = await self.nea.get("weather", {"api": "wbgt"})
        rec = f.data["records"][0]
        ok = [r for r in rec["item"]["readings"] if r.get("wbgt") not in (None, "") and r.get("heatStress")]
        if not ok:
            raise Unclear("wbgt")
        best = min(ok, key=lambda r: km(loc, _loc(r["location"])))
        return [(f"Heat stress: {str(best['heatStress']).lower()} (WBGT {float(best['wbgt']):.1f}C, "
                 f"{best['station']['name']}, {self._as_of(f, rec.get('datetime'))})")]

    async def _p_uv(self, area: str) -> list[str]:
        f = await self.nea.get("uv")
        rec = f.data["records"][0]
        latest = max(rec["index"], key=lambda x: x["hour"])
        at = self._time(latest["hour"])
        value = int(latest["value"])
        when = (f"{at:%H:%M}" if at.date() == self.now().date() else f"{at:%a %H:%M}") if at else "?"
        stale = f", NEA not answering now, {_age(f.age_s(self._clock()))} old" if f.stale else ""
        note = "" if at is None or self.now() - at < timedelta(hours=2) else " (NEA gives UV in daylight hours only)"
        return [f"UV index: {value} ({air.uv_band(value)}), island-wide, at {when}{stale}{note}"]

    async def _p_air(self, area: str) -> list[str]:
        psi_f, pm_f = await asyncio.gather(self.nea.get("psi"), self.nea.get("pm25"))
        if _REGION.get():
            regions: tuple[str, ...] = (_REGION.get(),)
        else:
            regions = self.home_regions if area == self.home else (await self._region(area, psi_f),)
        psi_item = psi_f.data["items"][0]
        pm_item = pm_f.data["items"][0]
        readings = psi_item["readings"]
        lines: list[str] = []
        worst = None
        psi_stale = f", {self._as_of(psi_f, psi_item.get('timestamp'))}" if psi_f.stale else ""
        for reg in regions:
            pm = pm_item["readings"]["pm25_one_hourly"].get(reg)
            psi = readings["psi_twenty_four_hourly"].get(reg)
            if not isinstance(pm, (int, float)) or not isinstance(psi, (int, float)):
                lines.append(f"Air, {reg}: NEA has no reading for this region right now")
                continue
            psi = int(psi)
            subs = {k: readings[k][reg] for k in air.OTHER_SUB_INDICES if reg in (readings.get(k) or {})}
            value, by = air.aqi(pm, subs)
            worst = max(worst or 0, psi)
            lines += [f"Air, {reg} ({self._as_of(pm_f, pm_item.get('timestamp'))}):",
                      f"AQI {value} {air.aqi_band(value)} (aqicn-style, {by} now)",
                      f"PSI {psi} {air.psi_band(psi)} (NEA 24h{psi_stale})",
                      f"PM2.5 1h {pm} ({air.pm25_1h_band(int(pm))})"]
        if worst is not None:
            lines.append(f"NEA advice for healthy people: {air.psi_advice(worst)}.")
        return lines

    async def _region(self, area: str, psi_f: Fetched) -> str:
        _, loc = await self._area_loc(area)
        meta = psi_f.data["regionMetadata"]
        return min((m for m in meta if m["name"] != "national"), key=lambda m: km(loc, _loc(m["labelLocation"])))["name"]

    async def _p_forecast_24h(self, area: str) -> list[str]:
        f = await self.nea.get("twenty-four-hr-forecast")
        rec = f.data["records"][0]
        g = rec["general"]
        reg = _REGION.get() or (self.home_regions[0] if area == self.home
                                else await self._region(area, await self.nea.get("psi")))
        valid = (g.get("validPeriod") or {}).get("text") or "next 24 hours"
        lines = [(f"24h forecast, {valid} ({self._as_of(f, rec.get('updatedTimestamp'))}): {g['forecast']['text']}, "
                  f"{g['temperature']['low']}-{g['temperature']['high']}C, humidity "
                  f"{g['relativeHumidity']['low']}-{g['relativeHumidity']['high']}%, wind {g['wind']['direction']} "
                  f"{g['wind']['speed']['low']}-{g['wind']['speed']['high']} km/h")]
        for p in rec["periods"]:
            lines.append(f"{p['timePeriod']['text']}: {p['regions'][reg]['text']} ({reg})")
        return lines

    async def _p_outlook_4day(self, area: str) -> list[str]:
        f = await self.nea.get("four-day-outlook")
        rec = f.data["records"][0]
        lines = [f"Next 4 days, island-wide ({self._as_of(f, rec.get('updatedTimestamp'))}):"]
        for d in rec["forecasts"]:
            fc = d["forecast"]
            lines.append(f"{d['day']}: {fc.get('summary') or fc.get('text')}, {d['temperature']['low']}-"
                         f"{d['temperature']['high']}C")
        return lines

    async def _p_lightning(self, area: str) -> list[str]:
        _, loc = await self._area_loc(area)
        now = self.now()
        days = [now.date()] + ([now.date() - timedelta(days=1)] if now.hour == 0 and now.minute < LIGHTNING_MIN else [])
        since = now - timedelta(minutes=LIGHTNING_MIN)
        hits: list[tuple[float, str]] = []
        last: Fetched | None = None
        for day in days:
            last = f = await self.nea.get("weather", {"api": "lightning", "date": f"{day:%Y-%m-%d}"}, fresh_s=120)
            for rec in f.data["records"]:
                for r in rec["item"]["readings"] or []:
                    at = self._time(r.get("datetime"))
                    dist = km(loc, _loc(r["location"]))
                    if at and at >= since and dist <= LIGHTNING_KM:
                        hits.append((dist, "ground" if r.get("type") == "G" else "cloud"))
        if last and last.stale and last.age_s(self._clock()) > 300:
            return [f"Lightning: cannot tell, NEA not answering now (last data {_age(last.age_s(self._clock()))} old)"]
        stale = ", NEA not answering now" if last and last.stale else ""
        if not hits:
            return [f"Lightning: none within {LIGHTNING_KM:.0f} km in the last {LIGHTNING_MIN} min{stale}"]
        ground = sum(1 for _, t in hits if t == "ground")
        return [(f"Lightning: {len(hits)} strike(s) within {LIGHTNING_KM:.0f} km in the last {LIGHTNING_MIN} min "
                 f"({ground} cloud-to-ground), nearest {min(d for d, _ in hits):.1f} km{stale}")]


def _rain(mm_5min: float) -> str:
    rate = mm_5min * 12                          # mm per hour
    if rate <= 0:
        return "no rain"
    if rate < 2.5:
        return f"light rain ({mm_5min:g} mm)"
    if rate < 7.6:
        return f"moderate rain ({mm_5min:g} mm)"
    return f"heavy rain ({mm_5min:g} mm)"


def _age(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min"
