"""Is a phone on the home Wi-Fi? Plain TCP connects, no raw sockets (the container drops
every capability), and the kernel's ARP table for MAC addresses.

How one probe reads (Linux): connecting to a LAN address makes the kernel ask "who has
this IP?" (ARP). A phone that is there gets the SYN and answers at once: RST from a closed
port (ConnectionRefused) or an accept (ANSWERED). An address nobody answers for fails with
EHOSTUNREACH after ~3 s (GONE). In between, connect can hang to the timeout (SILENT): a
device that drops SYNs, OR a phone that just left while the kernel still holds a recent
entry for it (packets go straight out for ~8 s until the entry fails, and that connect
just times out). So SILENT is ambiguous: the tracker counts it as there (a departed phone
reads GONE one round later, well inside the 15 min away rule), and pairing counts only
ANSWERED (a Wi-Fi toggle of 10 s would otherwise never look like "gone").

Phones use a private Wi-Fi address per network ("Private Wi-Fi Address" on iPhone,
"Randomised MAC" on Android), fixed unless set to rotate, so the MAC is what identifies a
phone; its IP can change when the DHCP lease renews.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from pathlib import Path

logger = logging.getLogger(__name__)

PROBE_PORT = 62078          # iPhone's sync port: accepted on iPhones, refused (RST) elsewhere
PROBE_TIMEOUT_S = 5.0       # > the ~3 s ARP failure (GONE); longer only means SILENT
SWEEP_TIMEOUT_S = 3.0       # sweep: only to make the kernel ARP every address; read the table after
SWEEP_SETTLE_S = 10.0       # ...then wait: a recent entry for a departed device stays "complete" ~8 s
ANSWERED, SILENT, GONE = "answered", "silent", "gone"
ROUTES = Path("/proc/net/route")
SWEEP_CONCURRENCY = 128
ARP_TABLE = Path("/proc/net/arp")
ATF_COM = 0x2               # ARP entry complete: the address answered


def outcome(error: BaseException | None) -> str:
    """What one connect's outcome says about the host (see the module docstring)."""
    if error is None or isinstance(error, (ConnectionRefusedError, ConnectionResetError)):
        return ANSWERED
    if isinstance(error, TimeoutError):
        return SILENT
    # Unreachable, no route, out of sockets: not seen this round; a phone counts as away only
    # after many rounds in a row, so a glitch costs nothing
    return GONE


def gateway(text: str) -> str | None:
    """/proc/net/route -> the default gateway's IPv4 address."""
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 2 and parts[1] == "00000000" and parts[2] != "00000000":
            try:
                return socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
            except (ValueError, OSError):
                return None
    return None


def parse_arp(text: str) -> dict[str, str]:
    """/proc/net/arp -> {ip: mac} for complete entries only."""
    out: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        ip, flags, mac = parts[0], parts[2], parts[3].lower()
        try:
            if int(flags, 16) & ATF_COM and mac != "00:00:00:00:00:00":
                out[ip] = mac
        except ValueError:
            continue
    return out


def normal_mac(mac: str) -> str | None:
    """'AA-BB-...' / 'aabb...' -> 'aa:bb:cc:dd:ee:ff', or None."""
    hexes = "".join(c for c in mac.lower() if c in "0123456789abcdef")
    if len(hexes) != 12 or len(mac) > 17:
        return None
    return ":".join(hexes[i:i + 2] for i in range(0, 12, 2))


def private_mac(mac: str) -> bool:
    """A randomised (locally administered) address, as phones use per Wi-Fi network."""
    return bool(int(mac[:2], 16) & 0x02)


class Net:
    """The real network. Tests use a fake with the same four methods."""

    def __init__(self, subnet: str = "", port: int = PROBE_PORT, arp_path: Path = ARP_TABLE,
                 routes: Path = ROUTES):
        self.port = port
        self.arp_path = arp_path
        self.routes = routes
        self._subnet = subnet
        self._route_warned = False

    def hosts(self) -> list[str]:
        """Addresses to sweep: PRESENCE_SUBNET, else the Pi's own address as a /24."""
        net = ipaddress.ip_network(self._subnet or f"{own_ip()}/24", strict=False)
        return [str(h) for h in net.hosts()]

    async def probe(self, ip: str, timeout: float = PROBE_TIMEOUT_S) -> str:
        writer = None
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(ip, self.port), timeout)
            error: BaseException | None = None
        except (TimeoutError, OSError) as e:
            error = e
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
        return outcome(error)

    async def lan_ok(self) -> bool:
        """Does the router still answer? If not, the Pi's own network is down and every phone
        would look gone: the tracker skips the round instead of calling everyone away."""
        try:
            gw = gateway(await asyncio.to_thread(self.routes.read_text))
        except OSError as e:
            if not self._route_warned:
                self._route_warned = True
                logger.warning("Cannot read the route table (%s); presence cannot spot a network outage", e)
            return True
        if gw is None:
            return False                 # no default route: a Pi on Wi-Fi whose access point is down
        return await self.probe(gw) != GONE

    async def arp(self) -> dict[str, str]:
        try:
            return parse_arp(await asyncio.to_thread(self.arp_path.read_text))
        except OSError as e:
            logger.warning("Cannot read the ARP table: %s", e)
            return {}

    async def sweep(self) -> dict[str, str]:
        """Touch every address so the kernel ARPs it, then read who answered: {mac: ip}."""
        sem = asyncio.Semaphore(SWEEP_CONCURRENCY)

        async def touch(ip: str) -> None:
            async with sem:
                await self.probe(ip, SWEEP_TIMEOUT_S)

        await asyncio.gather(*(touch(ip) for ip in self.hosts()))
        await asyncio.sleep(SWEEP_SETTLE_S)      # departed devices' recent entries fail meanwhile
        return {mac: ip for ip, mac in (await self.arp()).items()}


def own_ip() -> str:
    """The Pi's LAN address (no packet is sent: a UDP connect only picks the route)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
