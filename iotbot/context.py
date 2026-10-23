"""Wires settings, data files, the Broadlink hub and services together."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from iotbot.ac.service import AcService
from iotbot.ac.state import AcStateStore
from iotbot.config import Settings
from iotbot.devices.hub import BroadlinkHub, Expected, Transport
from iotbot.devices.model import Registry, load_registry
from iotbot.llm.assistant import Assistant
from iotbot.llm.client import LlmClient
from iotbot.llm.diary import DiaryTools
from iotbot.llm.home import HomeTools
from iotbot.llm.memory import Memory, MemoryTools
from iotbot.llm.replay import ReplayLog
from iotbot.llm.world import HolidayTools, OutsideTools
from iotbot.outside.holidays import Holidays
from iotbot.outside.nea import NeaClient
from iotbot.outside.weather import Outside
from iotbot.presence.net import Net
from iotbot.presence.offers import Offers
from iotbot.presence.tracker import Presence, validate_phones
from iotbot.schedule.notify import Notifier
from iotbot.schedule.runner import Scheduler
from iotbot.schedule.service import ScheduleService
from iotbot.schedule.store import ScheduleStore
from iotbot.services.devices import DeviceService
from iotbot.services.users import UserService, validate_users
from iotbot.store import JsonlLog, JsonStore, StoreError, read_json
from iotbot.telemetry import Tracker

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    settings: Settings
    registry: Registry
    hub: BroadlinkHub
    devices: DeviceService
    users: UserService
    ac: AcService | None = None
    schedules: ScheduleService | None = None
    scheduler: Scheduler | None = None
    notifier: Notifier | None = None
    tracker: Tracker | None = None
    assistant: Assistant | None = None          # None: LLM_ENABLED=off
    memory: Memory | None = None
    outside: Outside | None = None
    holidays: Holidays | None = None
    presence: Presence | None = None             # None: PRESENCE=off or no AC encoder
    offers: Offers | None = None
    started_at: float = field(default_factory=time.time)
    warnings: list[str] = field(default_factory=list)


def expected_devices(reg: Registry) -> list[Expected]:
    out: dict[str, Expected] = {}
    for room in reg.rooms.values():
        if room.rm_mac:
            out.setdefault(room.rm_mac, Expected(room.rm_mac, f"{room.name} RM", room.rm_type, room.rm_ip))
    for d in reg.devices.values():
        if d.kind == "plug":
            out.setdefault(d.mac, Expected(d.mac, d.id, d.bl_type))
    return list(out.values())


def assistant_devices(reg: Registry, ac: AcService | None) -> list[str]:
    """The devices the assistant may use: managed aircons only (toggle IR stays button-only)."""
    return [f"{d.id}: aircon in room '{d.room}'" for d in reg.devices.values() if ac and ac.manages(d.id)]


def build_context(settings: Settings, transport: Transport | None = None) -> AppContext:
    """Load all files. Raises StoreError/ValueError on unusable users.json (refuse to start)."""
    warnings: list[str] = []

    devices_cfg, w = read_json(settings.devices_path, dict)
    if w:
        warnings.append(w)
    try:
        commands = json.loads(settings.commands_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        warnings.append(f"{settings.commands_path.name} not found: no IR codes loaded")
        commands = {}
    except ValueError as e:
        warnings.append(f"{settings.commands_path.name} is not valid JSON ({e}): no IR codes loaded")
        commands = {}

    registry = load_registry(devices_cfg, commands)
    warnings += registry.warnings
    if not registry.devices:
        warnings.append(f"No devices loaded; add rooms and devices to {settings.devices_path.name}")

    users_store = JsonStore(settings.users_path, dict, validate=validate_users)
    users_store.load()
    if users_store.load_warning:
        warnings.append(users_store.load_warning)
    if not users_store.data:
        warnings.append(f"No approved users: send /ping to the bot and add your id to {settings.users_path.name}")

    hub = BroadlinkHub(transport or Transport(settings.discover_timeout))
    events = JsonlLog(settings.log_dir, "device_events", settings.timezone)
    audit = JsonlLog(settings.log_dir, "audit", settings.timezone)
    tracker = Tracker(JsonlLog(settings.log_dir, "turns", settings.timezone))

    ac = None
    if settings.ac_encoder:
        ac_store = AcStateStore(settings.state_dir / "ac_state.json")
        ac_store.load()
        if ac_store.load_warning:
            warnings.append(ac_store.load_warning)
        ac = AcService(registry, ac_store)
        warnings += ac.warnings
    else:
        logger.info("AC_ENCODER=off: aircons send their captured codes")

    devices = DeviceService(registry, hub, events, ac)
    users = UserService(users_store, audit, settings.admin_ids)
    if settings.admin_ids and not any(users.is_allowed(a) for a in settings.admin_ids):
        warnings.append("ADMIN_IDS lists no approved user: nobody can approve or remove users")

    # Schedules: a damaged file never stops the bot (moved aside, starts empty)
    sched_store = ScheduleStore(settings.state_dir / "schedules.json")
    sched_store.load()
    if sched_store.load_warning:
        warnings.append(sched_store.load_warning)
    warnings += [f"schedule {sid} is broken and will not run: {why}" for sid, why in sched_store.broken.items()]
    schedules = ScheduleService(sched_store, devices, JsonlLog(settings.log_dir, "schedule_events", settings.timezone),
                                settings.timezone)
    scheduler = Scheduler(sched_store, devices, settings.timezone,
                          floor=(lambda: ac.store.latest()) if ac else None)
    schedules.on_change = scheduler.wake
    scheduler.tracker = tracker

    def recipients(creator: int) -> list[int]:
        # The creator; if they are no longer approved, everyone, so nothing goes unseen
        if users.is_allowed(creator):
            return [creator]
        return [uid for uid, _ in users.list()]

    notifier = Notifier(schedules, scheduler, recipients, allowed=users.is_allowed)
    scheduler.notify = notifier.on_fire

    # NEA: keyless, read-only, also used by /outside when the LLM is off
    outside = Outside(NeaClient(), settings.timezone, home=settings.outside_area,
                      home_regions=settings.outside_regions)
    holidays = Holidays(settings.state_dir / "holidays.json", settings.timezone)
    if (w := holidays.load()):
        warnings.append(w)

    presence = offers = None
    if settings.presence and ac is not None:
        phones = JsonStore(settings.state_dir / "phones.json", dict, validate=validate_phones)
        try:
            phones.load()
            if phones.load_warning:
                warnings.append(phones.load_warning)
        except StoreError as e:
            # Not a cache, but losing it only means setting phones up again: never block startup
            phones.use_default()
            warnings.append(f"{e}; presence starts with no phones set up")
        presence = Presence(phones, Net(settings.presence_subnet),
                            JsonlLog(settings.log_dir, "presence_events", settings.timezone), users.is_allowed)
        offers = Offers(presence, devices, ac, registry, users.name_of, settings.timezone)
    elif settings.presence:
        logger.info("Presence needs the AC encoder (AC_ENCODER=on); switched off")

    assistant = memory = None
    if settings.llm_enabled:
        memory = Memory(settings.state_dir / "memory.json", settings.timezone)
        if (w := memory.load()):
            warnings.append(w)
        llm = LlmClient(settings.llm_url, None if settings.llm_slot == -1 else settings.llm_slot)
        assistant = Assistant(llm, lambda: assistant_devices(registry, ac), settings.timezone,
                              replay=ReplayLog(settings.log_dir / "llm", settings.timezone), memory=memory,
                              day_label=holidays.label,
                              aircons=lambda: [(d.id, d.room) for d in registry.devices.values()
                                               if ac and ac.manages(d.id)])
    else:
        logger.info("LLM_ENABLED=off: the chat assistant is switched off")

    ctx = AppContext(
        settings=settings, registry=registry, hub=hub,
        devices=devices, users=users, ac=ac,
        schedules=schedules, scheduler=scheduler, notifier=notifier, tracker=tracker, assistant=assistant, memory=memory,
        outside=outside, holidays=holidays, presence=presence, offers=offers, warnings=warnings,
    )
    if assistant is not None:
        home = HomeTools(ctx)
        assistant.add_tools(home.tools)
        assistant.add_tools(MemoryTools(memory, home).tools)
        assistant.add_tools(DiaryTools(ctx, memory).tools)
        assistant.add_tools(OutsideTools(outside).tools)
        assistant.add_tools(HolidayTools(holidays).tools)
    for w in warnings:
        logger.warning("Config: %s", w)
    return ctx
