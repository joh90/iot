"""Singapore public holidays, read-only (Phase 4.9; PLAN "Holidays").

MOM's list from data.gov.sg (collection 691, one dataset per year, with "Observed" Mondays
and ad-hoc days such as polling day), refreshed every REFRESH_DAYS and kept in
state/holidays.json. holidays_sg.json in this folder is the fallback when the list cannot be
fetched. Eves (CNY, Christmas, New Year) are not holidays and are only labelled. No schedule
logic: the bot answers questions and marks the dates it shows the model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from iotbot.store import write_json_atomic

logger = logging.getLogger(__name__)

FALLBACK = Path(__file__).with_name("holidays_sg.json")
COLLECTION_URL = "https://api-production.data.gov.sg/v2/public/api/collections/691/metadata"
DATASET_URL = "https://data.gov.sg/api/action/datastore_search"
REFRESH_DAYS = 30
CALL_GAP_S = 3.0                 # datastore search: 4 keyless calls per 10 s
PAGE = 100
MAX_PAGES = 10
DATE_RE = re.compile(r"^20\d\d-[01]\d-[0-3]\d$")
MAX_NAME = 60


def _clean(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    name = " ".join(name.replace("’", "'").replace("‘", "'").split())
    return name[:MAX_NAME] if name and name.isprintable() else None


def _rows(raw: Any) -> list[tuple[date, str]]:
    """[{"date": "2026-01-01", "name"|"holiday": "..."}] -> checked (date, name); bad rows are skipped."""
    out = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict) or not isinstance(r.get("date"), str) or not DATE_RE.match(r["date"]):
            continue
        name = _clean(r.get("name", r.get("holiday")))
        try:
            day = date.fromisoformat(r["date"])
        except ValueError:
            continue
        if name:
            out.append((day, name))
    return out


class Holidays:
    def __init__(self, path: Path, tz: str, http: httpx.AsyncClient | None = None,
                 clock: Callable[[], float] = time.time, fallback: Path = FALLBACK):
        self.path = path
        self.tz = ZoneInfo(tz)
        self._http = http
        self._clock = clock
        self.fallback = fallback
        self.days: dict[date, str] = {}
        self.fetched_at: float = 0.0
        self._builtin: dict[int, list[tuple[date, str]]] = {}
        # Per dataset: its rows. One dataset is usually one year, but collection 691 also holds a
        # 2020-2027 one, so a year is taken from the dataset that covers the fewest years.
        self._sets: dict[str, list[tuple[date, str]]] = {}

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:                           # made on first use: tests never make a real one
            self._http = httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "iotbot (home use)"})
        return self._http

    @http.setter
    def http(self, client: httpx.AsyncClient) -> None:
        self._http = client

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    # ---- loading -------------------------------------------------------------------------

    def load(self) -> str | None:
        """Built-in list, then the saved live list on top. Returns a warning or None."""
        warning = None
        try:
            raw = json.loads(self.fallback.read_text(encoding="utf-8"))
            self._builtin = {int(y): _rows(rows) for y, rows in (raw.get("years") or {}).items()}
        except (OSError, ValueError, AttributeError) as e:
            warning = f"holiday fallback list unreadable: {e}"
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            # Parsed into locals first: a damaged file changes nothing and is refreshed soon
            fetched_at = float(saved.get("fetched_at") or 0)
            sets = {str(k): _rows(v) for k, v in (saved.get("sets") or {}).items()}
            self._sets, self.fetched_at = sets, fetched_at
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError) as e:
            warning = f"{self.path.name} unreadable ({e}); using the built-in list until the next refresh"
        self._index()
        return warning

    def _index(self) -> None:
        years = dict(self._builtin)
        best: dict[int, int] = {}                         # year -> span of the dataset it came from
        for rows in self._sets.values():
            if not rows:
                continue
            span = max(d.year for d, _ in rows) - min(d.year for d, _ in rows)
            by_year: dict[int, list[tuple[date, str]]] = {}
            for d, n in rows:
                by_year.setdefault(d.year, []).append((d, n))
            for y, got in by_year.items():
                if y not in best or span < best[y]:
                    best[y] = span
                    years[y] = sorted(set(got))
        self.days = {d: n for rows in years.values() for d, n in rows}

    def stale(self) -> bool:
        return self._clock() - self.fetched_at > REFRESH_DAYS * 86400

    async def refresh(self, force: bool = False) -> bool:
        """Fetch MOM's list if it is older than REFRESH_DAYS. Never raises; True if anything changed.
        Datasets whose years are all past are not read again; one failing dataset does not stop
        the others (they are saved, and the failed one is tried again the next day)."""
        if not force and not self.stale():
            return False
        try:
            r = await self.http.get(COLLECTION_URL)
            r.raise_for_status()
            ids = r.json()["data"]["collectionMetadata"]["childDatasets"]
            if not isinstance(ids, list):
                raise TypeError("no dataset list")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            logger.warning("Holiday list refresh failed: %s", e)
            return False
        this_year = datetime.fromtimestamp(self._clock(), self.tz).year
        sets = dict(self._sets)
        failed = changed = 0
        for ds in ids:
            if not isinstance(ds, str) or not re.fullmatch(r"d_[0-9a-f]{32}", ds):
                continue
            old = sets.get(ds)
            if old and max(d.year for d, _ in old) < this_year:
                continue                                         # only past years: they do not change
            try:
                await asyncio.sleep(CALL_GAP_S)
                rows = await self._dataset(ds)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                logger.warning("Holiday dataset %s failed: %s", ds, e)
                failed += 1
                continue
            if rows and rows != old:
                sets[ds] = rows
                changed += 1
        self._sets = sets
        if not failed:
            self.fetched_at = self._clock()
        self._index()
        data = {"fetched_at": self.fetched_at,
                "sets": {k: [{"date": d.isoformat(), "name": n} for d, n in v] for k, v in sorted(sets.items())}}
        try:
            await asyncio.to_thread(write_json_atomic, self.path, data)
        except OSError as e:
            logger.warning("Could not save %s: %s", self.path.name, e)
        logger.info("Holiday list refreshed: %d dataset(s) changed, %d failed", changed, failed)
        return changed > 0

    async def _dataset(self, ds: str) -> list[tuple[date, str]]:
        """All rows of one dataset, page by page."""
        rows: list[tuple[date, str]] = []
        offset = 0
        for _ in range(MAX_PAGES):
            r = await self.http.get(DATASET_URL, params={"resource_id": ds, "limit": str(PAGE), "offset": str(offset)})
            r.raise_for_status()
            result = r.json()["result"]
            records = result["records"]
            rows += _rows(records)
            offset += len(records)
            if not records or offset >= int(result.get("total") or 0):
                break
            await asyncio.sleep(CALL_GAP_S)
        return sorted(set(rows))

    async def run(self) -> None:
        """Background: refresh when due, checked daily. Cancelled on shutdown."""
        while True:
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 -- the daily loop must survive any surprise
                logger.exception("Holiday refresh crashed")
            await asyncio.sleep(86400)

    # ---- questions -----------------------------------------------------------------------

    def today(self) -> date:
        return datetime.fromtimestamp(self._clock(), self.tz).date()

    def known_until(self) -> date | None:
        return max(self.days) if self.days else None

    def eves(self, year: int) -> dict[date, str]:
        out = {date(year, 12, 24): "Christmas Eve", date(year, 12, 31): "New Year's Eve"}
        cny = sorted(d for d, n in self.days.items() if d.year == year and n.startswith("Chinese New Year"))
        if cny:
            out[cny[0] - timedelta(days=1)] = "Chinese New Year's Eve"
        return {d: n for d, n in out.items() if d not in self.days}

    def label(self, day: date) -> str | None:
        """'PH: Deepavali (Observed)', 'eve: Christmas Eve, not a public holiday', or None."""
        if not self._covered(day):
            return f"PH list for {day.year} not known yet"
        if day in self.days:
            return f"PH: {self.days[day]}"
        eve = self.eves(day.year).get(day)
        return f"eve: {eve}, not a public holiday" if eve else None

    def _covered(self, day: date) -> bool:
        return day.year in {d.year for d in self.days}

    def day(self, day: date) -> str:
        if not self._covered(day):
            return f"{day:%a %d %b %Y}: I do not have MOM's holiday list for {day.year} yet"
        what = self.label(day) or "not a public holiday"
        weekend = " (a weekend)" if day.weekday() >= 5 and day not in self.days else ""
        return f"{day:%a %d %b %Y}: {what}{weekend}"

    def upcoming(self, start: date, count: int = 5) -> list[str]:
        rows = [f"{d:%a %d %b %Y}: {n}" for d, n in sorted(self.days.items()) if d >= start][:count]
        return rows or [f"No holidays known after {start:%d %b %Y}; MOM has not published the next list yet"]

    def year(self, year: int) -> list[str]:
        rows = [f"{d:%a %d %b}: {n}" for d, n in sorted(self.days.items()) if d.year == year]
        return rows or [f"I do not have MOM's holiday list for {year} (MOM publishes it mid-year before)"]

    def long_weekends(self, start: date, until: date) -> list[str]:
        """Runs of 3+ days off (Sat, Sun, public holidays) that include a public holiday; a run
        already under way at `start` counts. Stops where MOM's list ends, and says so."""
        out = []
        known = self.known_until()
        note = ""
        if known is None or start > known:
            return [f"I do not have MOM's holiday list for {start.year} yet"]
        if until > date(known.year, 12, 31):
            until = date(known.year, 12, 31)
            note = f"(MOM's list only goes to the end of {known.year})"
        while self._off(start - timedelta(days=1)):
            start -= timedelta(days=1)
        d = start
        while d <= until:
            if not self._off(d):
                d += timedelta(days=1)
                continue
            first = d
            while self._off(d + timedelta(days=1)):
                d += timedelta(days=1)
            names = sorted({self.days[x] for x in _days(first, d) if x in self.days})
            if (d - first).days >= 2 and names:
                out.append(f"{first:%a %d %b} to {d:%a %d %b %Y} ({(d - first).days + 1} days): {', '.join(names)}")
            d += timedelta(days=1)
        out = out or [f"No long weekends between {start:%d %b %Y} and {until:%d %b %Y}"]
        return out + ([note] if note else [])

    def _off(self, d: date) -> bool:
        return d.weekday() >= 5 or d in self.days


def _days(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]
