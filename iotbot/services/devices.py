"""Device actions. Every surface goes through DeviceService.run()."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass

from iotbot.devices.hub import BroadlinkHub, HubError
from iotbot.devices.model import Device, Registry, resolve_feature
from iotbot.result import Actor, Result
from iotbot.store import JsonlLog

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LastAction:
    """Latest action per device. `seq` is assigned when the action STARTS, so a later
    Undo can check "is this still the latest thing sent to the device?"."""
    seq: int
    feature: str
    at: float
    actor: str
    ok: bool | None          # None while in flight
    prev_feature: str | None


class DeviceService:
    def __init__(self, registry: Registry, hub: BroadlinkHub, events: JsonlLog):
        self.registry = registry
        self.hub = hub
        self.events = events
        self.last: dict[str, LastAction] = {}
        self._seq = itertools.count(1)

    def get(self, device_id: str) -> Device | None:
        return self.registry.devices.get(device_id)

    def resolve(self, device_id: str, words: list[str]) -> Result:
        """Turn `/d <device> <words...>` into (device, feature) or a helpful failure."""
        device = self.get(device_id)
        if device is None:
            return Result.fail("device_not_found", f"Device '{device_id}' not found. /list shows device ids.")
        if not words:
            return Result.fail("feature_missing", f"What should {device.id} do? Try: {_features_hint(device)}")
        feature = resolve_feature(device, words)
        if feature is None:
            return Result.fail("feature_not_found",
                               f"{device.id} has no '{' '.join(words)}'. Try: {_features_hint(device)}")
        return Result.success(data={"device": device.id, "feature": feature})

    async def run(self, device_id: str, feature: str, actor: Actor,
                  request_id: str | None = None, source: str | None = None) -> Result:
        """Run one feature. `request_id` correlates surfaces (e.g. a Telegram update);
        `source` links to what triggered it (e.g. a schedule event id). Both are logged."""
        device = self.get(device_id)
        if device is None:
            return Result.fail("device_not_found", f"Device '{device_id}' not found.")
        f = device.features.get(feature)
        if f is None:
            # Allowlist (B2): only features built from captured codes / known plug ops
            return Result.fail("feature_not_found", f"{device.id} has no '{feature}'.")

        prev = self.last.get(device.id)
        seq = next(self._seq)
        self.last[device.id] = LastAction(seq, feature, time.time(), actor.name, None,
                                          prev.feature if prev else None)
        started = time.perf_counter()
        state = None
        maybe = False
        ok, error = False, "cancelled"
        try:
            if device.kind == "plug":
                state = await self.hub.plug(device.mac, f.plug_op or feature)
            else:
                room = self.registry.rooms.get(device.room)
                if room is None or not room.rm_mac:
                    raise HubError(f"room '{device.room}' has no RM remote configured")
                await self.hub.send_ir(room.rm_mac, f.codes, f.gap_s, f.idempotent)
            ok, error = True, ""
        except HubError as e:
            ok, error, maybe = False, str(e), e.maybe_delivered
        except asyncio.CancelledError:
            ok, error, maybe = False, "cancelled", True
            raise
        except Exception as e:  # noqa: BLE001 -- report, never crash a handler
            logger.exception("Unexpected error running %s %s", device.id, feature)
            ok, error, maybe = False, f"unexpected error: {e.__class__.__name__}", True
        finally:
            ms = round((time.perf_counter() - started) * 1000)
            cur = self.last.get(device.id)
            if cur and cur.seq == seq:
                self.last[device.id] = LastAction(seq, feature, cur.at, actor.name, ok, cur.prev_feature)
            record = {
                "device": device.id, "room": device.room, "feature": feature, "seq": seq,
                "user_id": actor.user_id, "surface": actor.surface,
                "request_id": request_id, "source": source,
                "ok": ok, "error": error or None, "maybe_delivered": maybe if not ok else None,
                "state": state, "ms": ms,
            }
            # Shielded so a cancelled caller still leaves a trace of a maybe-sent command
            await asyncio.shield(self.events.append_async(record))

        if not ok:
            return Result.fail("send_failed", f"{device.id} {f.label} failed: {error}",
                               data={"device": device.id, "feature": feature, "seq": seq,
                                     "request_id": request_id, "maybe_delivered": maybe, "ms": ms})
        if device.kind == "plug":
            what = "nightlight" if "nightlight" in (f.plug_op or "") else "power"
            msg = f"{device.id} {what} is " + ("unknown" if state is None else "ON" if state else "OFF")
        else:
            msg = f"Sent {device.id} {f.label}"
        return Result.success(msg, data={"device": device.id, "feature": feature, "seq": seq,
                                         "request_id": request_id, "state": state, "ms": ms})


def _features_hint(device: Device, limit: int = 8) -> str:
    keys = list(device.features)
    hint = ", ".join(k.replace("_", " ") for k in keys[:limit])
    return hint + (", ..." if len(keys) > limit else "") if keys else "(no captured codes)"
