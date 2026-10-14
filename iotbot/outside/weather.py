"""Outside conditions for one place, worded in code (Phase 4.8).

Home is the configured area (2h forecast area) with air readings for the east AND central regions
(PLAN "Outside"); any other of NEA's 47 forecast areas can be named, and then its nearest
region and stations are used. Every part says when NEA measured it ("as of 01:00"), and
stale data (NEA not answering) says how old it is. The chat tool and /outside share this.
"""

from __future__ import annotations

import asyncio
import contextvars
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from iotbot.outside import air
from iotbot.outside.nea import Fetched, NeaClient, NeaError

HOME = "City"
HOME_REGIONS = ("east", "central")
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
    def __init__(self, nea: NeaClient, tz: str, clock: Callable[[], float] | None = None):
        self.nea = nea
        self.tz = ZoneInfo(tz)
        self._clock = clock or nea._clock

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), self.tz)

    # ---- public ------------------------------------------------------------------------

    async def report(self, area: str | None = None, parts: tuple[str, ...] | list[str] = SUMMARY,
                     region: str | None = None) -> dict[str, Any]:
        """{"area": ..., "lines": [...]} for the chat tool; parts that fail say so in their line.
        `region` (north/south/east/west/central) picks the air readings instead of the area's."""
        area = area if area in AREAS else HOME
        region = region if region in REGIONS else None
        token = _REGION.set(region)
        try:
            lines: list[str] = []
            for part in parts:
                lines += await self._part(area, part)
        finally:
            _REGION.reset(token)
        out = {"area": area + (" (home)" if area == HOME else ""), "lines": lines}
        if region:
            out["region"] = region
        return out

    async def summary(self, area: str | None = None) -> str:
        """The /outside message: plain text, one screen."""
        r = await self.report(area, SUMMARY)
        if any("thunder" in line.lower() for line in r["lines"][:1]):
            r["lines"][1:1] = await self._part(area if area in AREAS else HOME, "lightning")
        if area in AREAS and area != HOME:
            head = f"Outside, {area}:"
        else:
            head = f"Outside, home ({HOME}):"
        return head + "\n" + "\n".join(r["lines"])

    @staticmethod
    def match_area(text: str) -> str | None:
        """'/outside jurong east' -> 'Jurong East'; a unique prefix is enough ('toa' -> 'Toa Payoh')."""
        t = " ".join(text.lower().split())
        if not t:
            return None
        exact = [a for a in AREAS if a.lower() == t]
        if exact:
            return exact[0]
        hits = [a for a in AREAS if a.lower().startswith(t)]
        return hits[0] if len(hits) == 1 else None

    # ---- parts -------------------------------------------------------------------------

    async def _part(self, area: str, part: str) -> list[str]:
        fn = getattr(self, f"_p_{part}", None)
        if fn is None:
            return [f"{part}: not something I can look up"]
        try:
            return await fn(area)
        except NeaError as e:
            return [f"{part.replace('_', ' ')}: {e}"]
        except (Unclear, KeyError, IndexError, TypeError, ValueError):
            return [f"{part.replace('_', ' ')}: NEA's reply was not understood, so I cannot say"]

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
        text = next(x["forecast"] for x in item["forecasts"] if x["area"] == area)
        period = item.get("valid_period", {}).get("text") or "next 2 hours"
        return [f"Next 2h ({period}): {text} ({self._as_of(f, item.get('update_timestamp'))})"]

    async def _stations(self, feed: str, loc: tuple[float, float]) -> tuple[Fetched, str, float, float, str]:
        """Nearest station with a reading: (fetched, name, km, value, timestamp)."""
        f = await self.nea.get(feed)
        d = f.data
        reading = d["readings"][0]
        values = {x["stationId"]: x["value"] for x in reading["data"]}
        best = min(((km(loc, _loc(s["location"])), s) for s in d["stations"] if s["id"] in values),
                   key=lambda t: t[0], default=None)
        if best is None:
            raise Unclear(feed)
        return f, best[1]["name"], best[0], float(values[best[1]["id"]]), reading["timestamp"]

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
        wet = sum(1 for x in near if x["value"] and x["stationId"] in by_id
                  and km(loc, _loc(by_id[x["stationId"]]["location"])) <= 5)
        extra = f"; rain at {wet} station(s) within 5 km" if wet else "; no rain at stations within 5 km"
        return [f"Rain: {_rain(rain)} in the last 5 min ({name}, {dist:.0f} km{extra}, {self._as_of(f, ts)})"]

    async def _p_wind(self, area: str) -> list[str]:
        _, loc = await self._area_loc(area)
        (sf, name, _, speed, ts), (_, _, _, deg, _) = await asyncio.gather(
            self._stations("wind-speed", loc), self._stations("wind-direction", loc))
        compass = COMPASS[round(deg / 22.5) % 16]
        return [f"Wind: {speed:.0f} knots ({speed * 1.852:.0f} km/h) from the {compass} ({name}, {self._as_of(sf, ts)})"]

    async def _p_heat(self, area: str) -> list[str]:
        _, loc = await self._area_loc(area)
        f = await self.nea.get("weather", {"api": "wbgt"})
        rec = f.data["records"][0]
        best = min(rec["item"]["readings"], key=lambda r: km(loc, _loc(r["location"])))
        return [(f"Heat stress: {str(best['heatStress']).lower()} (WBGT {float(best['wbgt']):.1f}C, "
                 f"{best['station']['name']}, {self._as_of(f, rec.get('datetime'))})")]

    async def _p_uv(self, area: str) -> list[str]:
        f = await self.nea.get("uv")
        rec = f.data["records"][0]
        latest = max(rec["index"], key=lambda x: x["hour"])
        at = self._time(latest["hour"])
        value = int(latest["value"])
        when = (f"{at:%H:%M}" if at.date() == self.now().date() else f"{at:%a %H:%M}") if at else "?"
        stale = ", NEA not answering now" if f.stale else ""
        note = "" if at is None or self.now() - at < timedelta(hours=2) else " (NEA gives UV in daylight hours only)"
        return [f"UV index: {value} ({air.uv_band(value)}), island-wide, at {when}{stale}{note}"]

    async def _p_air(self, area: str) -> list[str]:
        psi_f, pm_f = await asyncio.gather(self.nea.get("psi"), self.nea.get("pm25"))
        if _REGION.get():
            regions: tuple[str, ...] = (_REGION.get(),)
        else:
            regions = HOME_REGIONS if area == HOME else (await self._region(area, psi_f),)
        psi_item = psi_f.data["items"][0]
        pm_item = pm_f.data["items"][0]
        readings = psi_item["readings"]
        lines, worst = [], 0
        for reg in regions:
            pm = pm_item["readings"]["pm25_one_hourly"][reg]
            psi = int(readings["psi_twenty_four_hourly"][reg])
            subs = {k: readings[k][reg] for k in air.OTHER_SUB_INDICES if reg in readings.get(k, {})}
            value, by = air.aqi(pm, subs)
            worst = max(worst, psi)
            lines += [f"Air, {reg} ({self._as_of(pm_f, pm_item.get('timestamp'))}):",
                      f"AQI {value} {air.aqi_band(value)} (aqicn-style, {by} now)",
                      f"PSI {psi} {air.psi_band(psi)} (NEA 24h)",
                      f"PM2.5 1h {pm} ({air.pm25_1h_band(int(pm))})"]
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
        reg = _REGION.get() or ("east" if area == HOME else await self._region(area, await self.nea.get("psi")))
        lines = [(f"Today ({self._as_of(f, rec.get('updatedTimestamp'))}): {g['forecast']['text']}, "
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
