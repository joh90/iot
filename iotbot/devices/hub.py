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
    """Could the device have executed the command despite this error?"""
    if isinstance(err, (blex.NetworkTimeoutError, socket.timeout, TimeoutError)):
        return True   # no reply: it may have run
    if isinstance(err, blex.BroadlinkException):
        return False  # the device replied with an error code: it did not run
    if isinstance(err, OSError):
        return False  # sendto failed (network unreachable etc.): never left this host
    return True       # unknown: assume the worst


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
        for dev in broadlink.xdiscover(timeout=timeout or self.discover_timeout,
                                       local_ip_address=local_ip()):
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
    def __init__(self, transport: Transport, sleep: Callable[[float], Any] = asyncio.sleep):
        self.transport = transport
        self._sleep = sleep
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

        by_ip = [m for m, e in self._expected.items() if e.ip]
        by_scan = {m for m, e in self._expected.items() if not e.ip}
        found: dict[str, Any] = {}
        if by_scan:
            try:
                found = await asyncio.to_thread(self.transport.scan, by_scan)
            except Exception as err:  # noqa: BLE001 -- network errors must not stop startup
                logger.error("Broadlink discovery failed: %s", err)
        for m in by_ip:
            dev = await self._hello(m)
            if dev is not None:
                found[m] = dev

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
        """Find one device again (fixed IP first, then broadcast) and re-auth it."""
        e = self._expected.get(mac)
        if e is None:
            return False
        dev = await self._hello(mac) if e.ip else None
        if dev is None:
            try:
                found = await asyncio.to_thread(self.transport.scan, {mac})
            except Exception as err:  # noqa: BLE001
                self._mark_offline(mac, f"rediscovery failed: {err}")
                return False
            dev = found.get(mac)
        if dev is None:
            self._mark_offline(mac, "not found on rediscovery")
            return False
        return await self._adopt(mac, dev)

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
            if op in ("power_on", "power_off"):
                on = op == "power_on"
                # Setting an absolute state is idempotent
                await self._call_with_retry(mac, "set_power", (on,), True, need="set_power", what="plug")
                return await self._call_with_retry(mac, "check_power", (), True,
                                                   need="check_power", what="plug")
            if op in ("nightlight_on", "nightlight_off"):
                on = op == "nightlight_on"
                await self._call_with_retry(mac, "set_nightlight", (on,), True,
                                            need="set_nightlight", what="plug with nightlight")
                return await self._call_with_retry(mac, "check_nightlight", (), True,
                                                   need="check_nightlight", what="plug with nightlight")
            if op in ("check_power", "check_nightlight"):
                return await self._call_with_retry(mac, op, (), True, need=op, what="plug")
        raise HubError(f"unknown plug operation '{op}'")

    def _lock(self, mac: str) -> asyncio.Lock:
        if mac not in self._expected:
            raise HubError(f"Broadlink device {mac} is not in devices.json")
        return self._locks.setdefault(mac, asyncio.Lock())

    async def _device(self, mac: str) -> Any:
        dev = self._devices.get(mac)
        if dev is None and await self.rediscover(mac):
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
        try:
            result = await asyncio.to_thread(self.transport.call, dev, method, *args)
        except Exception as err:  # noqa: BLE001
            maybe = maybe_delivered(err)
            if maybe and not idempotent:
                raise HubError(f"no reply from {self.status[mac].label}; it may or may not have "
                               f"received the command, not retrying ({err})", maybe_delivered=True) from err
            logger.warning("%s: %s failed (%s); rediscovering and retrying once",
                           self.status[mac].label, method, err)
            if not await self.rediscover(mac):
                raise HubError(f"{self.status[mac].label} is offline ({self.status[mac].error})",
                               maybe_delivered=maybe) from err
            dev = self._devices[mac]
            try:
                result = await asyncio.to_thread(self.transport.call, dev, method, *args)
            except Exception as err2:  # noqa: BLE001
                self._mark_offline(mac, f"{method} failed twice: {err2}")
                raise HubError(f"{self.status[mac].label} failed twice: {err2}",
                               maybe_delivered=maybe or maybe_delivered(err2)) from err2
        self.status[mac].last_ok = time.time()
        return result
