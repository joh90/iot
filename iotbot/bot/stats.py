"""/stats: speed, failures and LLM health from the logs (Phase 6).

Reads `turns` (one line per interaction) and `device_events` (one line per send) for the
last N days. Percentiles use the nearest-rank method and are hidden below MIN_N samples:
a p95 of three taps says nothing.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

import httpx

from iotbot.bot.text import b, h
from iotbot.devices.model import Registry
from iotbot.store import JsonlLog

DEFAULT_DAYS, MAX_DAYS = 7, 365
FAST_MS = 300            # PLAN.md speed target: slash -> IR send under 300 ms
MIN_N = 5
SLOWEST = 3
PROBE_TIMEOUT_S = 1.0
PROBE_TOTAL_S = 2.0
# Plain words for the household
SURFACES = {"button": "buttons", "slash": "commands", "text": "chat", "scheduler": "schedules"}
# Only what summary() reads: a year of lines with raw text would not fit a Pi 3 comfortably
TURN_KEYS = ("surface", "total_ms", "outcome", "allowed", "user_id", "lag_s")
EVENT_KEYS = ("device", "ms", "ok", "surface", "user_id", "error", "maybe_delivered")
NOT_WORK = ("denied", "dropped_offline")   # never reached a handler: no speed to measure


def read_since(log: JsonlLog, since: datetime, now: datetime,
               keys: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """Every record from `since` to `now`, reading only the monthly files in that range.
    `keys`: keep only these fields of each record."""
    out: list[dict[str, Any]] = []
    y, m = since.astimezone(log.tz).year, since.astimezone(log.tz).month
    end = (now.astimezone(log.tz).year, now.astimezone(log.tz).month)
    while (y, m) <= end:
        for rec in log.read_month(y, m):
            try:
                if datetime.fromisoformat(rec["ts"]) >= since:
                    out.append({k: rec.get(k) for k in keys} if keys else rec)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue                     # a line without a usable timestamp
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile of a non-empty list."""
    s = sorted(values)
    return s[max(1, math.ceil(len(s) * p / 100)) - 1]


def _typical_slow(values: list[float], unit: str) -> str:
    if len(values) < MIN_N:
        return f"{len(values)}x, too few to tell"
    return f"{pct(values, 50):.0f} / {pct(values, 95):.0f} {unit} ({len(values)}x)"


def _ms(rec: dict[str, Any], key: str) -> float | None:
    v = rec.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def sender_label(registry: Registry, device_id: str) -> str:
    """The Broadlink box that did the sending: the room's RM for IR, the plug itself."""
    d = registry.devices.get(device_id)
    if d is None:
        return device_id
    return device_id if d.kind == "plug" else f"{d.room} remote"


def summary(turns: Iterable[dict[str, Any]], events: Iterable[dict[str, Any]], registry: Registry,
            days: int, name_of: Callable[[int], str]) -> list[str]:
    turns, events = list(turns), list(events)
    lines = [b(f"Stats, last {days} day{'s' if days != 1 else ''}") + f" ({len(turns)} turns)"]

    # Speed: time inside the bot, per surface
    lines += ["", b("Speed inside the bot") + " (typical / slowest 1 in 20)"]
    work = [t for t in turns if t.get("outcome") not in NOT_WORK]
    any_speed = False
    for surface, label in SURFACES.items():
        totals = [v for t in work if t.get("surface") == surface and (v := _ms(t, "total_ms")) is not None]
        if not totals:
            continue
        any_speed = True
        fast = sum(v < FAST_MS for v in totals) * 100 // len(totals)
        lines.append(f"- {label}: {_typical_slow(totals, 'ms')}, {fast}% under {FAST_MS} ms")
    if not any_speed:
        lines.append("- nothing yet")
    ir = [v for e in events if e.get("ok") and (v := _ms(e, "ms")) is not None]
    if ir:
        lines.append(f"- IR / plug send: {_typical_slow(ir, 'ms')}")
    lags = [v for t in work if (v := _ms(t, "lag_s")) is not None and v >= 0]
    if lags:
        lines.append(f"- message delay (Telegram to bot): {_typical_slow(lags, 's')}")

    # Slowest senders: p95 of successful sends, only with enough samples
    by_sender: dict[str, list[float]] = defaultdict(list)
    for e in events:
        if e.get("ok") and (v := _ms(e, "ms")) is not None and isinstance(e.get("device"), str):
            by_sender[sender_label(registry, e["device"])].append(v)
    ranked = sorted(((pct(v, 95), k, len(v)) for k, v in by_sender.items() if len(v) >= MIN_N), reverse=True)
    lines += ["", b("Slowest remotes / plugs") + " (slowest 1 in 20 sends)"]
    lines += [f"- {h(k)}: {p95:.0f} ms ({n}x)" for p95, k, n in ranked[:SLOWEST]] or \
             [f"- not enough sends yet (need {MIN_N} per remote)"]

    # Failures: from device_events, so scheduler sends count too
    failed = [e for e in events if e.get("ok") is False]
    lines += ["", b("Failed sends") + f" ({len(failed)})"]
    per_device: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in failed:
        per_device[str(e.get("device"))].append(e)
    for dev, es in sorted(per_device.items(), key=lambda kv: -len(kv[1])):
        who = Counter("schedule" if e.get("surface") == "scheduler"
                      else name_of(e["user_id"]) if isinstance(e.get("user_id"), int) else "unknown"
                      for e in es)
        maybe = sum(bool(e.get("maybe_delivered")) for e in es)
        last = str(es[-1].get("error") or "")[:80]
        lines.append(f"- {h(dev)}: {len(es)} ({h(', '.join(f'{n} {w}' for w, n in who.most_common()))})"
                     + (f", {maybe} may have arrived" if maybe else "") + (f"; last: {h(last)}" if last else ""))
    if not failed:
        lines.append("- none")

    # Odds and ends
    strangers = [t for t in turns if t.get("allowed") is False and t.get("surface") != "scheduler"]
    dropped = sum(t.get("outcome") == "dropped_offline" for t in turns)
    errors = sum(t.get("outcome") == "error" for t in turns)
    lines += ["", b("Other"),
              f"- strangers: {len(strangers)} message(s) from {len({t.get('user_id') for t in strangers})} id(s)",
              f"- dropped while the bot was offline: {dropped}",
              f"- bot errors: {errors}"]
    return lines


async def llm_health(url: str, client: httpx.AsyncClient | None = None,
                     clock: Callable[[], float] = time.perf_counter) -> str:
    """One line: up/down, loaded model, time to answer. Never raises, never takes over PROBE_TOTAL_S
    (httpx timeouts are per phase, so a half-open tunnel could otherwise hold it much longer)."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=PROBE_TIMEOUT_S)
    try:
        async with asyncio.timeout(PROBE_TOTAL_S):
            return await _probe(url, client, clock)
    except TimeoutError:
        return "down (no answer in 2 s)"
    except Exception as e:  # noqa: BLE001 -- a bad LLM_URL must not cost the whole /stats reply
        return f"down ({e.__class__.__name__})"
    finally:
        if own:
            await client.aclose()


async def _probe(url: str, client: httpx.AsyncClient, clock: Callable[[], float]) -> str:
    started = clock()
    try:
        r = await client.get(f"{url}/health")
    except httpx.HTTPError as e:
        return f"down ({e.__class__.__name__})"
    ms = round((clock() - started) * 1000)
    if r.status_code != 200:
        return f"not ready (HTTP {r.status_code}, {ms} ms)"
    model = "model unknown"
    try:
        data = (await client.get(f"{url}/v1/models")).json().get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("id"):
            model = str(data[0]["id"])
    except (httpx.HTTPError, ValueError, AttributeError):
        pass
    return f"up, {h(model)}, {ms} ms"
