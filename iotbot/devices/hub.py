"""Async access to Broadlink devices (RM remotes and SP plugs).

- Startup never dies on one device: each expected MAC is found and authed on its own (B9).
- Devices are matched by MAC, so a DHCP address change is handled by rediscovery.
  An optional fixed IP per device (DHCP reservation) skips the broadcast.
- Any device with `send_data` can send IR, not just type "RMMINI" (B7).
- One asyncio lock per device serializes packets (the library's own lock is a no-op:
  `with self.lock and socket(...)` only enters the socket).
- Retry policy: rediscover + re-auth + retry once, but only when a retry cannot
  double an action. Idempotent packets (full-state AC frames) always qualify. Toggles
  (e.g. TV power) are retried only if the failure proves the device never ran the
  command (it answered with an error, or the packet never left this host). A timeout
  means "maybe ran", so toggles are not retried after one.

Note: broadlink itself re-sends a packet every 1s until it gets a reply within its
timeout. A lost reply can therefore still repeat a toggle; we keep that timeout short.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import broadlink
from broadlink import exceptions as blex

logger = logging.getLogger(__name__)

SEND_TIMEOUT = 3  # seconds the library waits for a reply (it re-sends every 1s meanwhile)
REDISCOVER_BACKOFF = 20.0  # seconds before another broadcast for a device that was not found

# Errors where the device answered "I did not do it" before executing anything
_NOT_RUN = (
    blex.AuthenticationError, blex.AuthorizationError, blex.ConnectionClosedError,
    blex.DeviceOfflineError, blex.CommandNotSupportedError, blex.StructureAbnormalError,
)


class HubError(Exception):
    """A device action failed. `maybe_delivered` is True if it might have run anyway."""

    def __init__(self, message: str, maybe_delivered: bool = False):
        super().__init__(message)
        self.maybe_delivered = maybe_delivered


def mac_of(dev: Any) -> str:
    return bytes(dev.mac).hex()


def local_ip() -> str | None:
    """IP of the interface with the default route (multi-NIC hosts broadcast on the wrong one otherwise)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def maybe_delivered(err: BaseException) -> bool:
    """Could the device have executed the command despite this error?

    Only explicit pre-execution refusals count as "did not run". Timeouts, malformed
    replies (DataValidationError arrives after the device answered) and socket errors
    (the library re-sends every 1s, so an earlier copy may have landed) are all "maybe".
    """
    return not isinstance(err, _NOT_RUN)


@dataclass(slots=True)
class Expected:
    mac: str
    label: str
    want_type: str = ""      # from config, informational only
    ip: str = ""             # optional fixed IP


@dataclass(slots=True)
class DeviceStatus:
    mac: str
    label: str
    online: bool = False
    type: str = ""
    ip: str = ""
    error: str = ""
    last_ok: float | None = None
    warnings: list[str] = field(default_factory=list)


class Transport:
    """Thin blocking wrapper over the broadlink library, swappable in tests."""

    def __init__(self, discover_timeout: float):
        self.discover_timeout = discover_timeout

    def scan(self, want: set[str], timeout: float | None = None) -> dict[str, Any]:
        """Broadcast; return {mac: device} for wanted MACs, stopping early once all are seen."""
        found: dict[str, Any] = {}
        gen = broadlink.xdiscover(timeout=timeout or self.discover_timeout, local_ip_address=local_ip())
        with contextlib.closing(gen):  # closes the socket when we stop early
            for dev in gen:
                m = mac_of(dev)
                if m in want:
                    found[m] = dev
                    if found.keys() >= want:
                        break
        return found

    def hello(self, ip: str, timeout: float | None = None) -> Any:
        return broadlink.hello(ip, timeout=timeout or self.discover_timeout)

    def auth(self, dev: Any) -> None:
        dev.timeout = SEND_TIMEOUT
        dev.auth()

    def call(self, dev: Any, method: str, *args: Any) -> Any:
        return getattr(dev, method)(*args)


class BroadlinkHub:
    def __init__(self, transport: Transport, sleep: Callable[[float], Any] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.transport = transport
        self._sleep = sleep
        self._clock = clock
        self._last_miss: dict[str, float] = {}
        self._expected: dict[str, Expected] = {}
        self._devices: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.status: dict[str, DeviceStatus] = {}

    # ---- setup -------------------------------------------------------------------------

    async def start(self, expected: Iterable[Expected]) -> None:
        """Find and auth every expected device; failures are recorded, never raised."""
        for e in expected:
            self._expected[e.mac] = e
            self._locks.setdefault(e.mac, asyncio.Lock())
            self.status[e.mac] = DeviceStatus(e.mac, e.label)

        found: dict[str, Any] = {}
        for m in [m for m, e in self._expected.items() if e.ip]:
            dev = await self._hello(m)
            if dev is not None:
                found[m] = dev
        # Broadcast for everything else, including fixed-IP devices that did not answer
        by_scan = set(self._expected) - set(found)
        if by_scan:
            try:
                found.update(await asyncio.to_thread(self.transport.scan, by_scan))
            except Exception as err:  # noqa: BLE001 -- network errors must not stop startup
                logger.error("Broadlink discovery failed: %s", err)

        for m in self._expected:
            if m in found:
                await self._adopt(m, found[m])
            else:
                self._mark_offline(m, "not found on the network")

    async def _hello(self, mac: str) -> Any | None:
        e = self._expected[mac]
        try:
            dev = await asyncio.to_thread(self.transport.hello, e.ip)
        except Exception as err:  # noqa: BLE001
            logger.warning("%s: no reply at fixed IP %s (%s)", e.label, e.ip, err)
            return None
        if mac_of(dev) != mac:
            logger.warning("%s: %s answered with MAC %s, expected %s", e.label, e.ip, mac_of(dev), mac)
            return None
        return dev

    async def _adopt(self, mac: str, dev: Any) -> bool:
        e = self._expected[mac]
        st = self.status[mac]
        try:
            await asyncio.to_thread(self.transport.auth, dev)
        except Exception as err:  # noqa: BLE001
            self._mark_offline(mac, f"auth failed: {err}")
            return False
        self._devices[mac] = dev
        st.online, st.error = True, ""
        st.type = str(getattr(dev, "type", "") or "")
        st.ip = str(dev.host[0]) if getattr(dev, "host", None) else ""
        st.last_ok = time.time()
        st.warnings = []
        if e.want_type and st.type and e.want_type != st.type:
            st.warnings.append(f"config says {e.want_type}, device reports {st.type}")
        logger.info("Broadlink %s ready: %s %s at %s", e.label, st.type, mac, st.ip)
        return True

    def _mark_offline(self, mac: str, why: str) -> None:
        self._devices.pop(mac, None)
        st = self.status[mac]
        st.online, st.error = False, why
        logger.warning("Broadlink %s offline: %s", st.label, why)

    async def rediscover(self, mac: str) -> bool:
        """Public: find one device again under its lock (e.g. a /status refresh)."""
        if mac not in self._expected:
            return False
        async with self._locks[mac]:
            return await self._rediscover(mac, force=True)

    async def _rediscover(self, mac: str, force: bool = False) -> bool:
        """Caller holds the device lock. Fixed IP first, then broadcast, then re-auth."""
        e = self._expected[mac]
        last = self._last_miss.get(mac)
        if not force and last is not None and self._clock() - last < REDISCOVER_BACKOFF:
            return False  # don't broadcast on every command for a device that is truly gone
        dev = await self._hello(mac) if e.ip else None
        if dev is None:
            try:
                found = await asyncio.to_thread(self.transport.scan, {mac})
            except Exception as err:  # noqa: BLE001
                self._mark_offline(mac, f"rediscovery failed: {err}")
                return False
            dev = found.get(mac)
        if dev is None:
            self._last_miss[mac] = self._clock()
            self._mark_offline(mac, "not found on rediscovery")
            return False
        ok = await self._adopt(mac, dev)
        if ok:
            self._last_miss.pop(mac, None)
        else:
            self._last_miss[mac] = self._clock()
        return ok

    # ---- actions -----------------------------------------------------------------------

    async def send_ir(self, mac: str, packets: tuple[bytes, ...], gap_s: float = 0.0,
                      idempotent: bool = False) -> None:
        """Send IR packets in order through the remote with this MAC. Raises HubError."""
        if not packets:
            raise HubError("no IR code captured for this action")
        lock = self._lock(mac)
        async with lock:
            for i, pkt in enumerate(packets):
                if i and gap_s:
                    await self._sleep(gap_s)
                await self._call_with_retry(mac, "send_data", (pkt,), idempotent,
                                            need="send_data", what="IR remote")

    async def plug(self, mac: str, op: str) -> bool | None:
        """Run a plug operation. Returns the reported state for check_* / set_* ops."""
        lock = self._lock(mac)
        async with lock:
            # Setting an absolute state is idempotent. If only the read-back fails, the
            # set still worked: report unknown state (None) rather than an error.
            for prefix, setter, checker, what in (("power", "set_power", "check_power", "plug"),
                                                  ("nightlight", "set_nightlight", "check_nightlight",
                                                   "plug with nightlight")):
                if op in (f"{prefix}_on", f"{prefix}_off"):
                    await self._call_with_retry(mac, setter, (op.endswith("_on"),), True,
                                                need=setter, what=what)
                    try:
                        return await self._call_with_retry(mac, checker, (), True, need=checker, what=what)
                    except HubError as e:
                        logger.warning("%s set ok but read-back failed: %s", mac, e)
                        return None
            if op in ("check_power", "check_nightlight"):
                return await self._call_with_retry(mac, op, (), True, need=op, what="plug")
        raise HubError(f"unknown plug operation '{op}'")

    def _lock(self, mac: str) -> asyncio.Lock:
        if mac not in self._expected:
            raise HubError(f"Broadlink device {mac} is not in devices.json")
        return self._locks.setdefault(mac, asyncio.Lock())

    async def _device(self, mac: str) -> Any:
        dev = self._devices.get(mac)
        if dev is None and await self._rediscover(mac):
            dev = self._devices.get(mac)
        if dev is None:
            st = self.status[mac]
            raise HubError(f"{st.label} is offline ({st.error or 'not found'})")
        return dev

    async def _call_with_retry(self, mac: str, method: str, args: tuple, idempotent: bool,
                               need: str, what: str) -> Any:
        dev = await self._device(mac)
        if not hasattr(dev, need):
            raise HubError(f"{self.status[mac].label} ({self.status[mac].type}) is not a {what}")
        label = self.status[mac].label
        try:
            result = await self._run_thread(dev, method, args)
        except Exception as err:  # noqa: BLE001
            if not isinstance(err, (blex.BroadlinkException, OSError)):
                # A programming error (e.g. wrong arguments), not a network fault: keep the device online
                raise HubError(f"{label}: {method} error: {err}", maybe_delivered=True) from err
            maybe = maybe_delivered(err)
            if maybe and not idempotent:
                # No resend, but recover the session/IP so the next command works
                await self._rediscover(mac, force=True)
                raise HubError(f"no clear reply from {label}; it may or may not have run the command, "
                               f"not resending a toggle ({err})", maybe_delivered=True) from err
            logger.warning("%s: %s failed (%s); rediscovering and retrying once", label, method, err)
            if not await self._rediscover(mac, force=True):
                raise HubError(f"{label} is offline ({self.status[mac].error})", maybe_delivered=maybe) from err
            dev = self._devices[mac]
            try:
                result = await self._run_thread(dev, method, args)
            except Exception as err2:  # noqa: BLE001
                self._mark_offline(mac, f"{method} failed twice: {err2}")
                raise HubError(f"{label} failed twice: {err2}",
                               maybe_delivered=maybe or maybe_delivered(err2)) from err2
        self.status[mac].last_ok = time.time()
        return result

    async def _run_thread(self, dev: Any, method: str, args: tuple) -> Any:
        """Run a blocking call; if we are cancelled, still wait for the thread so the
        device lock is never released while the Device object is mid-packet."""
        task = asyncio.ensure_future(asyncio.to_thread(self.transport.call, dev, method, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await task
            raise
