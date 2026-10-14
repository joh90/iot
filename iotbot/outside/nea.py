"""NEA real-time feeds from data.gov.sg v2: keyless, cached, throttled (Phase 4.8).

data.gov.sg allows 6 keyless real-time calls per 10 s; we keep to MAX_CALLS per WINDOW_S.
Each reply is cached FRESH_S. When NEA fails (down, slow, 429, bad JSON) the last good
reply is used up to STALE_MAX_S old, marked stale with its age (PLAN "NEA down").
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE = "https://api-open.data.gov.sg/v2/real-time/api/"
FRESH_S = 600
STALE_MAX_S = 6 * 3600
MAX_CALLS = 5
WINDOW_S = 10.0
TIMEOUT_S = 5.0              # the whole call (connect + read), so a chat turn stays under its 20 s cap
MAX_WAIT_S = WINDOW_S + 1.0   # one full window: a slot always comes; then give up
STALE_WAIT_S = 1.0           # with stale data in hand, wait at most this long for a slot
BACKOFF_S = 60.0             # after a failure, use stale data (or fail) without calling NEA again


class NeaError(Exception):
    """No usable data: NEA failed and nothing cached is young enough."""


@dataclass(slots=True)
class Fetched:
    data: dict[str, Any]
    at: float                  # when we fetched it
    stale: bool = False        # NEA failed just now; this is the last good reply

    def age_s(self, now: float) -> float:
        return now - self.at


class NeaClient:
    def __init__(self, http: httpx.AsyncClient | None = None, clock: Callable[[], float] = time.time,
                 base: str = BASE):
        self.http = http or httpx.AsyncClient(timeout=TIMEOUT_S, headers={"User-Agent": "iotbot (home use)"})
        self.base = base
        self._clock = clock
        self._cache: dict[str, Fetched] = {}
        self._calls: deque[float] = deque()
        self._locks: dict[str, asyncio.Lock] = {}
        self._failed: dict[str, float] = {}                # key -> when NEA last failed for it
        self.calls = 0                                      # real calls made (for tests and /stats)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def get(self, feed: str, params: dict[str, str] | None = None, fresh_s: float = FRESH_S) -> Fetched:
        """`feed` e.g. "psi" or "weather" (with params {"api": "lightning"}). Raises NeaError."""
        key = feed + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        async with self._locks.setdefault(key, asyncio.Lock()):    # one fetch per feed at a time
            hit = self._cache.get(key)
            now = self._clock()
            if hit is not None and hit.age_s(now) < fresh_s:
                return Fetched(hit.data, hit.at)
            usable = hit is not None and hit.age_s(now) <= STALE_MAX_S
            try:
                if now - self._failed.get(key, -BACKOFF_S) < BACKOFF_S:
                    raise NeaError("failed just now")
                if not await self._slot(STALE_WAIT_S if usable else MAX_WAIT_S):
                    raise NeaError("busy")
                self.calls += 1
                async with asyncio.timeout(TIMEOUT_S):
                    r = await self.http.get(self.base + feed, params=params)
                if r.status_code != 200:
                    raise NeaError(f"HTTP {r.status_code}")
                body = r.json()
                if not isinstance(body, dict) or body.get("code") not in (0, None) or not isinstance(
                        body.get("data"), dict):
                    raise NeaError(f"bad reply: {str(body.get('errorMsg') if isinstance(body, dict) else '')[:80]}")
            except (httpx.HTTPError, ValueError, NeaError, TimeoutError) as e:
                if str(e) != "failed just now":
                    logger.info("NEA %s failed: %s", feed, e or e.__class__.__name__)
                    if str(e) != "busy":
                        self._failed[key] = now
                if usable:
                    return Fetched(hit.data, hit.at, stale=True)
                raise NeaError("cannot reach NEA right now") from None
            got = Fetched(body["data"], self._clock())
            self._cache[key] = got
            self._failed.pop(key, None)
            return got

    async def _slot(self, max_wait: float = MAX_WAIT_S) -> bool:
        """Wait for a throttle slot (at most `max_wait` s); False if none came."""
        deadline = time.monotonic() + max_wait
        while True:
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= WINDOW_S:
                self._calls.popleft()
            if len(self._calls) < MAX_CALLS:
                self._calls.append(now)
                return True
            wait = WINDOW_S - (now - self._calls[0]) + 0.05
            if now + wait > deadline:
                return False
            await asyncio.sleep(wait)
