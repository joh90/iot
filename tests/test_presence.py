"""Phase 8 presence: probes, home/away, pairing, offers, /phone (fake LAN and clock, no IR)."""

import asyncio
import errno
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from iotbot.ac.state import AcState
from iotbot.bot import handlers as hd
from iotbot.bot import phone as ph
from iotbot.bot.callbacks import decode
from iotbot.presence import net as pn
from iotbot.presence import tracker as pt
from iotbot.result import Actor
from tests.test_ac_service import make_ctx
from tests.test_bot import ME, OTHER, make_context, make_update, replied

SGT = ZoneInfo("Asia/Singapore")
T0 = datetime(2026, 10, 23, 18, 0, tzinfo=SGT).timestamp()
PHONE, PHONE2, TV = "aa:00:00:00:00:01", "aa:00:00:00:00:02", "00:11:22:33:44:55"
ALICE = Actor(ME, "Alice", "button")
BOB = Actor(OTHER, "Bob", "button")


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class FakeLan:
    """ip -> mac for devices on the Wi-Fi now; `quiet` ips answer ARP but not TCP (still there)."""

    def __init__(self, devices=None):
        self.devices = dict(devices or {})
        self.sweeps = 0
        self.script = []          # pairing: one devices dict per probe round

    lan_up = True

    async def probe(self, ip):
        return pn.ANSWERED if ip in self.devices else pn.GONE

    async def lan_ok(self):
        return self.lan_up

    async def arp(self):
        return dict(self.devices)

    async def sweep(self):
        self.sweeps += 1
        return {mac: ip for ip, mac in self.devices.items()}


@pytest.fixture
def env(tmp_path):
    ctx = make_ctx(tmp_path)
    clock = Clock()
    lan = FakeLan({"192.168.1.20": PHONE, "192.168.1.30": TV})
    p = ctx.presence
    p.net, p._clock, p.started = lan, clock, clock.t
    ctx.offers._clock = clock
    sent, edits = [], []

    async def send(uid, text, rows, silent):
        sent.append(SimpleNamespace(uid=uid, text=text, rows=rows, silent=silent, mid=len(sent) + 1))
        return len(sent)

    async def edit(uid, mid, text, rows):
        edits.append(SimpleNamespace(uid=uid, mid=mid, text=text, rows=rows))

    ctx.offers.send, ctx.offers.edit = send, edit
    return SimpleNamespace(ctx=ctx, p=p, o=ctx.offers, lan=lan, clock=clock, sent=sent, edits=edits)


def labels(rows):
    return [t for row in rows for t, _ in row]


def data_for(rows, label):
    return next(d for row in rows for t, d in row if t == label)


async def tap(env, actor, data):
    parts = decode(data)
    return await env.o.tap(actor, parts[1], parts[2:])


# ---- net -------------------------------------------------------------------------------

@pytest.mark.parametrize("error,want", [
    (None, pn.ANSWERED), (ConnectionRefusedError(), pn.ANSWERED), (ConnectionResetError(), pn.ANSWERED),
    (TimeoutError(), pn.SILENT), (OSError(errno.EHOSTUNREACH, "No route to host"), pn.GONE),
    (OSError(errno.ENETUNREACH, "x"), pn.GONE), (OSError(errno.EMFILE, "Too many open files"), pn.GONE),
])
def test_what_a_connect_says(error, want):
    assert pn.outcome(error) == want


def test_gateway_from_the_route_table():
    text = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
            "eth0\t0001A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\n"
            "eth0\t00000000\t0101A8C0\t0003\t0\t0\t0\t00000000\n")
    assert pn.gateway(text) == "192.168.1.1" and pn.gateway("header\n") is None


def test_parse_arp_keeps_complete_entries():
    text = ("IP address       HW type     Flags       HW address            Mask     Device\n"
            "192.168.1.20     0x1         0x2         AA:00:00:00:00:01     *        wlan0\n"
            "192.168.1.21     0x1         0x0         00:00:00:00:00:00     *        wlan0\n"
            "192.168.1.22     0x1         0x6         aa:00:00:00:00:02     *        wlan0\n")
    assert pn.parse_arp(text) == {"192.168.1.20": PHONE, "192.168.1.22": PHONE2}


def test_mac_helpers_and_hosts():
    assert pn.normal_mac("AA-00-00-00-00-01") == PHONE and pn.normal_mac("zz") is None
    assert pn.private_mac(PHONE) and not pn.private_mac(TV)
    assert pn.Net("192.168.1.0/30").hosts() == ["192.168.1.1", "192.168.1.2"]


async def test_real_probe_on_localhost():
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        assert await pn.Net(port=port).probe("127.0.0.1", 2) == pn.ANSWERED      # accepted
    assert await pn.Net(port=port).probe("127.0.0.1", 2) == pn.ANSWERED          # closed now: refused


# ---- home / away -----------------------------------------------------------------------

async def pair(env, uid=ME, mac=PHONE, ip="192.168.1.20"):
    await env.p.pair(uid, mac, ip)


async def test_home_then_away_then_back(env):
    arrived, back, out = [], [], []
    await pair(env)
    env.p.on_arrive = AsyncMock(side_effect=lambda u: arrived.append(u))
    env.p.on_back = AsyncMock(side_effect=lambda u: back.append(u))
    env.p.on_all_away = AsyncMock(side_effect=lambda s: out.append(s))
    await env.p.tick()
    assert env.p.counts() == (1, 1)
    del env.lan.devices["192.168.1.20"]                        # left
    env.clock.t += 14 * 60
    await env.p.tick()
    assert env.p.is_home(ME) and not out                       # 14 min: a locked phone, not gone
    left_at = env.clock.t - 14 * 60
    env.clock.t += 2 * 60
    await env.p.tick()
    assert env.p.person(ME).state == "away" and out == [left_at]
    env.clock.t += 60
    await env.p.tick()
    assert out == [left_at]                                    # told once per stretch
    env.clock.t += 20 * 60
    env.lan.devices["192.168.1.20"] = PHONE
    await env.p.tick()
    assert back == [ME] and arrived == [ME]                    # 37 min after the last sighting
    log = [json.loads(line) for f in (env.ctx.settings.log_dir).glob("presence_events-*")
           for line in f.read_text().splitlines()]
    assert [e["event"] for e in log] == ["away", "home"] and log[1]["away_s"] == 37 * 60


async def test_short_absence_is_not_an_arrival(env):
    env.p.on_arrive = AsyncMock()
    await pair(env)
    await env.p.tick()
    del env.lan.devices["192.168.1.20"]
    env.clock.t += 20 * 60
    await env.p.tick()                                         # away (20 min)
    env.lan.devices["192.168.1.20"] = PHONE
    env.clock.t += 60
    await env.p.tick()
    env.p.on_arrive.assert_not_called()                        # 21 min < 30


async def test_after_a_restart_nobody_is_offered_anything_at_once(env):
    env.p.on_arrive = AsyncMock()
    env.p.on_all_away = AsyncMock()
    await pair(env)
    env.p.people.clear()                                       # as after a restart
    del env.lan.devices["192.168.1.20"]
    await env.p.tick()
    assert env.p.person(ME).state == "unknown"
    env.clock.t += 16 * 60
    await env.p.tick()
    assert env.p.person(ME).state == "away"
    env.p.on_all_away.assert_not_called()                      # never seen since the restart: no invented "since"
    env.lan.devices["192.168.1.20"] = PHONE
    env.clock.t += 60
    await env.p.tick()
    env.p.on_arrive.assert_not_called()                        # 17 min since start, not 30 away


async def test_a_phone_whose_ip_changed_is_found_by_a_sweep(env):
    await pair(env)
    env.lan.devices = {"192.168.1.20": TV, "192.168.1.44": PHONE}      # DHCP moved it
    env.clock.t += pt.SWEEP_EVERY_S
    await env.p.tick()
    assert env.p.is_home(ME) and env.p.phone(ME)["ip"] == "192.168.1.44" and env.lan.sweeps == 1
    del env.lan.devices["192.168.1.44"]
    await env.p.tick()
    assert env.lan.sweeps == 1                                 # at most one sweep per 5 min


async def test_removed_users_and_unpaired_phones_do_not_count(env):
    await env.ctx.users.add(ALICE, OTHER, "Bob")
    await pair(env)
    await pair(env, OTHER, PHONE2, "192.168.1.21")
    env.p.person(OTHER).state = "away"
    assert env.p.counts() == (1, 2)
    await env.ctx.users.delete(ALICE, OTHER)
    assert env.p.counts() == (1, 1)
    assert await env.p.unpair(ME) and env.p.counts() == (0, 0)


async def test_one_phone_one_person(env):
    await env.ctx.users.add(ALICE, OTHER, "Bob")
    await pair(env)
    await pair(env, OTHER)                                     # Bob pairs the same MAC
    assert env.p.phone(ME) is None and env.p.phone(OTHER)["mac"] == PHONE


def test_validate_phones():
    pt.validate_phones({"111": {"mac": PHONE, "ip": "192.168.1.2"}})
    for bad in ([], {"x": {}}, {"111": {"mac": "AA:00:00:00:00:01", "ip": "1"}}, {"111": {"mac": PHONE}}):
        with pytest.raises(TypeError):
            pt.validate_phones(bad)


async def test_forget_history_removes_only_mine(env):
    await env.p._log(ME, "away")
    await env.p._log(OTHER, "away")
    await env.p._log(ME, "home")
    assert await env.p.forget_history(ME) == 2
    lines = [json.loads(line) for f in env.ctx.settings.log_dir.glob("presence_events-*")
             for line in f.read_text().splitlines()]
    assert [e["user_id"] for e in lines] == [OTHER]


# ---- pairing ---------------------------------------------------------------------------

class TogglingLan(FakeLan):
    """Pairing: the phone answers, then drops off for two rounds, then comes back."""

    def __init__(self, togglers):
        super().__init__({"192.168.1.20": PHONE, "192.168.1.21": PHONE2, "192.168.1.30": TV})
        self.togglers = togglers
        self.round = 0

    async def arp(self):
        self.round += 1
        devices = dict(self.devices)
        if self.round in (2, 3):
            for ip in self.togglers:
                devices.pop(ip)
        return devices

    async def probe(self, ip):
        if ip in self.togglers and self.round + 1 in (2, 3):
            return pn.SILENT                                   # just switched off: the kernel's entry is recent
        return pn.ANSWERED


async def watch(env, lan):
    env.p.net = lan

    async def sleep(_):
        env.clock.t += 3

    env.p._sleep = sleep
    ready = AsyncMock()
    found = await env.p.watch_for_toggle(ready)
    ready.assert_awaited_once_with(3)
    return found


async def test_pairing_finds_the_device_that_dropped_off_and_came_back(env):
    assert await watch(env, TogglingLan(["192.168.1.20"])) == [(PHONE, "192.168.1.20")]


async def test_pairing_with_two_togglers_or_none(env):
    assert len(await watch(env, TogglingLan(["192.168.1.20", "192.168.1.21"]))) == 2
    assert await watch(env, TogglingLan([])) == []             # runs to the 2 min limit


async def test_pairing_skips_phones_already_set_up_and_one_at_a_time(env):
    await env.ctx.users.add(ALICE, OTHER, "Bob")
    await pair(env, OTHER, PHONE, "192.168.1.20")
    lan = TogglingLan(["192.168.1.20"])
    env.p.net = lan
    env.p._sleep = AsyncMock(side_effect=lambda _: setattr(env.clock, "t", env.clock.t + 3))
    assert await env.p.watch_for_toggle() == []                # Bob's phone is not offered to Alice
    async with env.p._pair_lock:
        with pytest.raises(RuntimeError):
            await env.p.watch_for_toggle()


# ---- offers ----------------------------------------------------------------------------

async def set_ac(env, device, **state):
    r = await env.ctx.devices.send_ac_state(device, AcState(**{"power": True, "temp": 24, **state}), BOB)
    assert r.ok


async def test_on_since_survives_changes_and_resets_on_off(env):
    ac = env.ctx.ac
    await set_ac(env, "bed_ac")
    first = ac.last("bed_ac").on_since
    await set_ac(env, "bed_ac", temp=25)
    assert ac.last("bed_ac").on_since == first
    await set_ac(env, "bed_ac", power=False)
    assert ac.last("bed_ac").on_since is None


async def test_arrival_offer_turns_on_with_the_last_state_and_undo(env):
    await pair(env)
    await set_ac(env, "office_ac", temp=26, power=False)
    await env.o.arrive(ME)
    (m,) = env.sent
    assert m.uid == ME and not m.silent and m.text == "Welcome home (18:00).\nBoth aircons are off."
    assert labels(m.rows) == ["Bedroom", "Office", "Both", "No thanks"]
    sent_before = len(env.ctx.sent)
    assert (await tap(env, ALICE, data_for(m.rows, "Office"))).ok
    assert env.ctx.ac.last("office_ac").state == AcState(power=True, temp=26)         # last state, on
    assert len(env.ctx.sent) == sent_before + 1
    e = env.edits[-1]
    assert "Office on, ON cool 26C" in e.text and "(by you, 18:00)" in e.text
    assert labels(e.rows) == ["Undo", "Bedroom too"]
    assert (await tap(env, ALICE, data_for(e.rows, "Undo"))).ok
    assert not env.ctx.ac.last("office_ac").state.power and "Undone (by you" in env.edits[-1].text
    assert labels(env.edits[-1].rows) == []


async def test_arrival_rules(env):
    await pair(env)
    await env.p.set_pref(ME, "arrival", False)
    await env.o.arrive(ME)
    assert env.sent == []                                      # switched off in /phone
    await env.p.set_pref(ME, "arrival", True)
    await set_ac(env, "bed_ac")
    await set_ac(env, "office_ac")
    await env.o.arrive(ME)
    assert env.sent == []                                      # everything is already on


async def come_home(env, *ips, after=40 * 60):
    for ip in ips:
        env.lan.devices.pop(ip, None)
    env.clock.t += 20 * 60
    await env.p.tick()
    env.clock.t += after - 20 * 60
    for ip, mac in zip(ips, (PHONE, PHONE2)):
        env.lan.devices[ip] = mac


async def test_arrival_only_to_an_empty_home_but_two_together_both_get_one(env):
    await env.ctx.users.add(ALICE, OTHER, "Bob")
    env.p.on_arrive = AsyncMock()
    await pair(env)
    await pair(env, OTHER, PHONE2, "192.168.1.21")
    env.lan.devices["192.168.1.21"] = PHONE2
    await come_home(env, "192.168.1.20")                       # Alice out, Bob stays home
    await env.p.tick()
    env.p.on_arrive.assert_not_called()                        # Bob was home
    await come_home(env, "192.168.1.20", "192.168.1.21")       # both out, both back in one round
    await env.p.tick()
    assert sorted(c.args[0] for c in env.p.on_arrive.await_args_list) == [ME, OTHER]


async def test_a_router_outage_is_not_everyone_leaving(env):
    env.p.on_all_away = AsyncMock()
    await pair(env)
    await env.p.tick()
    env.lan.lan_up = False
    env.lan.devices.clear()
    env.clock.t += 40 * 60
    await env.p.tick()
    assert env.p.is_home(ME)
    env.lan.lan_up = True
    await env.p.tick()                                         # back up, phone napping this round
    assert env.p.is_home(ME)                                   # the outage did not count as time away
    env.p.on_all_away.assert_not_called()


async def test_silent_counts_as_there_in_tracking(env):
    await pair(env)
    env.lan.probe = AsyncMock(return_value=pn.SILENT)          # answers ARP, drops the SYN
    env.clock.t += 60 * 60
    await env.p.tick()
    assert env.p.is_home(ME)


async def test_a_never_seen_housemate_does_not_block_departure(env):
    await env.ctx.users.add(ALICE, OTHER, "Bob")
    env.p.on_all_away = AsyncMock()
    await pair(env)
    await pair(env, OTHER, PHONE2, "192.168.1.21")
    env.p.people[OTHER] = pt.Person()                          # Bob away on a trip since before the restart
    await env.p.tick()
    del env.lan.devices["192.168.1.20"]
    env.clock.t += 16 * 60
    await env.p.tick()
    env.p.on_all_away.assert_awaited_once_with(T0)             # since Alice's last sighting


async def test_net_lan_ok_reads_the_route_table(tmp_path):
    routes = tmp_path / "route"
    routes.write_text("Iface\tDestination\tGateway\n")
    assert await pn.Net(routes=routes).lan_ok() is False       # no default route: Wi-Fi down
    assert await pn.Net(routes=tmp_path / "missing").lan_ok() is True


async def test_expiry_runs_even_when_a_round_fails(env):
    env.p.tick = AsyncMock(side_effect=RuntimeError("disk full"))
    env.p.on_round = AsyncMock()

    async def stop(_):
        raise asyncio.CancelledError

    env.p._sleep = stop
    with pytest.raises(asyncio.CancelledError):
        await env.p.run_forever()
    env.p.on_round.assert_awaited_once()


async def test_pairing_a_phone_closes_an_open_departure(env):
    await pair(env)
    await set_ac(env, "bed_ac")
    await env.o.all_away(env.clock.t)
    await env.ctx.users.add(ALICE, OTHER, "Bob")
    await pair(env, OTHER, PHONE2, "192.168.1.21")
    assert env.edits[-1].text.endswith("Offer closed.")


async def test_arrival_no_thanks_and_expiry(env):
    await pair(env)
    await env.o.arrive(ME)
    r = await tap(env, ALICE, data_for(env.sent[0].rows, "No thanks"))
    assert r.ok and "Left off (by you" in env.edits[-1].text and env.ctx.sent == []
    await env.o.arrive(ME)
    env.clock.t += 31 * 60
    await env.o.expire()
    assert env.edits[-1].text.endswith("Offer expired.") and env.edits[-1].rows == []


async def test_departure_goes_to_everyone_and_the_first_tap_wins(env):
    await env.ctx.users.add(ALICE, OTHER, "Bob")
    await pair(env)
    await pair(env, OTHER, PHONE2, "192.168.1.21")
    await env.ctx.ac.store.put("bed_ac", AcState(power=True, temp=24), "Bob", at=env.clock.t)
    env.clock.t += 90 * 60
    await env.ctx.ac.store.put("office_ac", AcState(power=True, temp=24), "Bob", at=env.clock.t)
    await env.o.all_away(env.clock.t - 60)
    a, b_ = env.sent
    assert {a.uid, b_.uid} == {ME, OTHER} and not a.silent
    assert a.text == "Everyone is out (since 19:29).\nStill on: Bedroom (since 18:00), Office (since 19:30)"
    assert labels(a.rows) == ["Turn off all", "Bedroom off", "Office off", "Leave on"]
    taps = await asyncio.gather(tap(env, BOB, data_for(b_.rows, "Turn off all")),
                                tap(env, ALICE, data_for(a.rows, "Turn off all")))
    assert sorted(r.ok for r in taps) == [False, True]          # one wins, the other is closed/already
    assert not env.ctx.ac.last("bed_ac").state.power and not env.ctx.ac.last("office_ac").state.power
    mine = [e for e in env.edits if e.uid == ME][-1]
    theirs = [e for e in env.edits if e.uid == OTHER][-1]
    assert "Turned off all (by Bob, 19:30)." in mine.text and "Turned off all (by you, 19:30)." in theirs.text
    assert labels(mine.rows) == ["Undo"]


async def test_departure_is_silent_at_night_and_closes_when_someone_is_back(env):
    await pair(env)
    await set_ac(env, "bed_ac")
    env.clock.t = datetime(2026, 10, 24, 2, 0, tzinfo=SGT).timestamp()
    await env.o.all_away(env.clock.t)
    assert env.sent[0].silent
    await env.o.back(ME)
    assert env.edits[-1].text.endswith("Someone is back (02:00). Offer closed.") and env.edits[-1].rows == []
    assert "Alice" not in env.edits[-1].text                   # counts only: never who came back


async def test_departure_needs_an_aircon_on_and_someone_who_wants_it(env):
    await pair(env)
    await env.o.all_away(env.clock.t)
    assert env.sent == []
    await set_ac(env, "bed_ac")
    await env.p.set_pref(ME, "departure", False)
    await env.o.all_away(env.clock.t)
    assert env.sent == []


async def test_offer_taps_are_only_for_who_got_them(env):
    await env.ctx.users.add(ALICE, OTHER, "Bob")
    await pair(env)
    await env.o.arrive(ME)
    r = await tap(env, BOB, data_for(env.sent[0].rows, "Bedroom"))
    assert r.error == "not_yours" and env.ctx.sent == []
    assert (await env.o.tap(ALICE, "on", ["nope", "bed_ac"])).error == "expired"


# ---- /phone and /status ----------------------------------------------------------------

async def test_phone_screens(env):
    u = make_update(ME)
    await hd.cmd_phone(u, make_context(env.ctx))
    assert "Your phone: not set up" in replied(u)
    await pair(env)
    u = make_update(ME)
    await hd.cmd_phone(u, make_context(env.ctx))
    assert "Your phone: set up</b>, home now" in replied(u) and "Home now: 1 of 1 phones" in replied(u)
    u = make_update(ME, data="ph:arr:0")
    await hd.on_button(u, make_context(env.ctx))
    assert not env.p.phone(ME)["arrival"]
    u = make_update(ME, data="ph:removey")
    await hd.on_button(u, make_context(env.ctx))
    assert env.p.phone(ME) is None


async def test_phone_setup_flow(env):
    env.p.net = TogglingLan(["192.168.1.20"])
    env.p._sleep = AsyncMock(side_effect=lambda _: setattr(env.clock, "t", env.clock.t + 3))
    bot = SimpleNamespace(edit_message_text=AsyncMock())
    await ph._setup(bot, env.p, ME, ME, 5)
    texts = [c.args[0] for c in bot.edit_message_text.call_args_list]
    assert "Turn your phone's Wi-Fi off" in texts[0] and "Is this your phone?" in texts[-1]
    u = make_update(ME, data="ph:yes:0")
    await hd.on_button(u, make_context(env.ctx))
    assert env.p.phone(ME)["mac"] == PHONE and env.p.is_home(ME)
    env.p.net = TogglingLan([])                                # a new setup that finds nothing
    await ph._setup(bot, env.p, ME, ME, 5)
    u = make_update(ME, data="ph:yes:0")                       # old Yes / forged: nothing pending
    await hd.on_button(u, make_context(env.ctx))
    (text,), _ = u.callback_query.answer.call_args
    assert "no longer active" in text


async def test_status_shows_counts_only(env):
    await pair(env)
    u = make_update(ME)
    await hd.cmd_status(u, make_context(env.ctx))
    assert "Home: 1 of 1 phones" in replied(u)


def test_presence_off(tmp_path):
    ctx = make_ctx(tmp_path, env={"PRESENCE": "off"})
    assert ctx.presence is None and ctx.offers is None


async def test_chat_home_state_has_counts_and_on_since(env):
    from iotbot.llm.home import HomeTools
    tools = {t.name: t for t in HomeTools(env.ctx).tools(ALICE)}
    out = await tools["get_home_state"].run({})
    assert "people_home" not in out                            # no phones set up
    await pair(env)
    await env.ctx.ac.store.put("bed_ac", AcState(power=True, temp=24), "Bob", at=T0)
    out = await tools["get_home_state"].run({})
    assert out["people_home"] == "1 of 1 phones set up are on the home Wi-Fi"
    assert out["aircons"][0]["on_since"] == "Fri 18:00" and "Alice" not in json.dumps(out)
