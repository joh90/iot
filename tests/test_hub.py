import pytest
from broadlink import exceptions as blex

from iotbot.devices.hub import BroadlinkHub, Expected, HubError, Transport

RM = "780f771abcde"
RM2 = "780f771abcdf"
PLUG = "780f77116def"


class FakeDev:
    def __init__(self, mac, type_="RMMINI", ip="10.0.0.5"):
        self.mac = bytes.fromhex(mac)
        self.type = type_
        self.host = (ip, 80)
        self.sent = []
        self.fail = []        # exceptions to raise on next calls
        self.power = False

    def send_data(self, pkt):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append(pkt)

    def set_power(self, on):
        if self.fail:
            raise self.fail.pop(0)
        self.power = on

    def check_power(self):
        return self.power


class FakePlug:
    def __init__(self, mac):
        self.mac = bytes.fromhex(mac)
        self.type = "SP2"
        self.host = ("10.0.0.9", 80)
        self.power = False
        self.fail = []

    def set_power(self, on):
        if self.fail:
            raise self.fail.pop(0)
        self.power = on

    def check_power(self):
        return self.power


class FakeTransport(Transport):
    def __init__(self, devices, hello_map=None):
        super().__init__(1)
        self.devices = {d.mac.hex(): d for d in devices}
        self.hello_map = hello_map or {}
        self.scans = 0
        self.auth_fail = set()

    def scan(self, want, timeout=None):
        self.scans += 1
        return {m: d for m, d in self.devices.items() if m in want}

    def hello(self, ip, timeout=None):
        if ip not in self.hello_map:
            raise blex.NetworkTimeoutError(-4000, "timeout", "x")
        return self.hello_map[ip]

    def auth(self, dev):
        if dev.mac.hex() in self.auth_fail:
            raise blex.AuthenticationError(-1, "auth", "x")

    def call(self, dev, method, *args):
        return getattr(dev, method)(*args)


async def nosleep(_):
    return None


async def make_hub(devs, expected, **kw):
    t = FakeTransport(devs, **kw)
    hub = BroadlinkHub(t, sleep=nosleep)
    await hub.start(expected)
    return hub, t


async def test_start_one_failure_does_not_stop_others():
    rm = FakeDev(RM)
    hub, t = await make_hub([rm, FakeDev(RM2)], [Expected(RM, "bedroom"), Expected(RM2, "study")])
    assert hub.status[RM].online
    # second one fails auth
    t2 = FakeTransport([rm, FakeDev(RM2)])
    t2.auth_fail.add(RM2)
    hub2 = BroadlinkHub(t2, sleep=nosleep)
    await hub2.start([Expected(RM, "bedroom"), Expected(RM2, "study")])
    assert hub2.status[RM].online and not hub2.status[RM2].online
    assert "auth failed" in hub2.status[RM2].error


async def test_missing_device_marked_offline():
    hub, _ = await make_hub([], [Expected(RM, "bedroom")])
    assert not hub.status[RM].online
    with pytest.raises(HubError, match="offline"):
        await hub.send_ir(RM, (b"\x26\x00",))


async def test_send_any_remote_type_b7():
    rm = FakeDev(RM, type_="RMMINIB")
    hub, _ = await make_hub([rm], [Expected(RM, "bedroom", want_type="RMMINI")])
    await hub.send_ir(RM, (b"\x26\x01",))
    assert rm.sent == [b"\x26\x01"]
    assert hub.status[RM].warnings  # type mismatch noted, not fatal


async def test_non_remote_rejected():
    hub, _ = await make_hub([FakePlug(RM)], [Expected(RM, "x")])
    with pytest.raises(HubError, match="not a IR remote"):
        await hub.send_ir(RM, (b"\x26",))


async def test_macro_packets_in_order():
    rm = FakeDev(RM)
    hub, _ = await make_hub([rm], [Expected(RM, "p")])
    await hub.send_ir(RM, (b"\x26\x01", b"\x26\x02"), gap_s=2.0)
    assert rm.sent == [b"\x26\x01", b"\x26\x02"]


async def test_idempotent_timeout_rediscovers_and_retries():
    rm = FakeDev(RM)
    hub, t = await make_hub([rm], [Expected(RM, "bedroom")])
    rm.fail = [blex.NetworkTimeoutError(-4000, "t", "x")]
    await hub.send_ir(RM, (b"\x26\x01",), idempotent=True)
    assert rm.sent == [b"\x26\x01"]
    assert t.scans == 2  # startup + rediscovery


async def test_toggle_timeout_not_retried():
    rm = FakeDev(RM)
    hub, _ = await make_hub([rm], [Expected(RM, "tv")])
    rm.fail = [blex.NetworkTimeoutError(-4000, "t", "x")]
    with pytest.raises(HubError) as ei:
        await hub.send_ir(RM, (b"\x26\x01",), idempotent=False)
    assert ei.value.maybe_delivered
    assert rm.sent == []


async def test_toggle_device_error_is_retried():
    rm = FakeDev(RM)
    hub, _ = await make_hub([rm], [Expected(RM, "tv")])
    rm.fail = [blex.AuthorizationError(-1, "auth", "x")]  # device answered: did not run
    await hub.send_ir(RM, (b"\x26\x01",), idempotent=False)
    assert rm.sent == [b"\x26\x01"]


async def test_failing_twice_marks_offline():
    rm = FakeDev(RM)
    hub, _ = await make_hub([rm], [Expected(RM, "bedroom")])
    rm.fail = [OSError("unreachable"), OSError("unreachable")]
    with pytest.raises(HubError, match="twice"):
        await hub.send_ir(RM, (b"\x26",), idempotent=True)
    assert not hub.status[RM].online


async def test_offline_device_found_on_next_send():
    rm = FakeDev(RM)
    t = FakeTransport([])
    hub = BroadlinkHub(t, sleep=nosleep)
    await hub.start([Expected(RM, "bedroom")])
    assert not hub.status[RM].online
    t.devices[RM] = rm  # it came back with a new DHCP address
    await hub.send_ir(RM, (b"\x26",))
    assert hub.status[RM].online and rm.sent


async def test_fixed_ip_uses_hello_not_broadcast():
    rm = FakeDev(RM, ip="10.0.0.50")
    hub, t = await make_hub([], [Expected(RM, "bedroom", ip="10.0.0.50")], hello_map={"10.0.0.50": rm})
    assert hub.status[RM].online
    assert t.scans == 0


async def test_fixed_ip_wrong_mac_falls_back_offline():
    other = FakeDev(RM2, ip="10.0.0.50")
    hub, _ = await make_hub([], [Expected(RM, "bedroom", ip="10.0.0.50")], hello_map={"10.0.0.50": other})
    assert not hub.status[RM].online


async def test_unknown_mac_rejected():
    hub, _ = await make_hub([], [])
    with pytest.raises(HubError, match="not in devices.json"):
        await hub.send_ir(RM, (b"\x26",))


async def test_plug_power_and_check():
    p = FakePlug(PLUG)
    hub, _ = await make_hub([p], [Expected(PLUG, "lamp")])
    assert await hub.plug(PLUG, "power_on") is True
    assert await hub.plug(PLUG, "check_power") is True
    assert await hub.plug(PLUG, "power_off") is False


async def test_plug_without_nightlight_rejected():
    hub, _ = await make_hub([FakePlug(PLUG)], [Expected(PLUG, "lamp")])
    with pytest.raises(HubError, match="nightlight"):
        await hub.plug(PLUG, "nightlight_on")


async def test_sends_serialized_per_device():
    import asyncio

    order = []

    class SlowDev(FakeDev):
        def send_data(self, pkt):
            import time
            order.append(("start", pkt))
            time.sleep(0.02)
            order.append(("end", pkt))

    rm = SlowDev(RM)
    hub, _ = await make_hub([rm], [Expected(RM, "b")])
    await asyncio.gather(hub.send_ir(RM, (b"\x26\x01",)), hub.send_ir(RM, (b"\x26\x02",)))
    assert [o[0] for o in order] == ["start", "end", "start", "end"]
