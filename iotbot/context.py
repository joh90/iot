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
from iotbot.schedule.notify import Notifier
from iotbot.schedule.runner import Scheduler
from iotbot.schedule.service import ScheduleService
from iotbot.schedule.store import ScheduleStore
from iotbot.services.devices import DeviceService
from iotbot.services.users import UserService, validate_users
from iotbot.store import JsonlLog, JsonStore, read_json

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
    users = UserService(users_store, audit)

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

    def recipients(creator: int) -> list[int]:
        # The creator; if they are no longer approved, everyone, so nothing goes unseen
        if users.is_allowed(creator):
            return [creator]
        return [uid for uid, _ in users.list()]

    notifier = Notifier(schedules, scheduler, recipients)
    scheduler.notify = notifier.on_fire

    ctx = AppContext(
        settings=settings, registry=registry, hub=hub,
        devices=devices, users=users, ac=ac,
        schedules=schedules, scheduler=scheduler, notifier=notifier,
        warnings=warnings,
    )
    for w in warnings:
        logger.warning("Config: %s", w)
    return ctx
