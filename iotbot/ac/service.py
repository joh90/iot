"""Full-state AC control: which state each button means, and what was last sent.

An AC is managed when its captured power_on decodes as a frame the encoder
understands; that capture is also the room's preset (what On sends). Anything
else (Daikin, odd captures, AC_ENCODER=off) keeps sending the captured codes.
"""

from __future__ import annotations

import asyncio
import logging

from iotbot.ac.mitsubishi import AcFrameError, build_packet, decode, packet_to_pulses, pulses_to_frames
from iotbot.ac.state import AcState, AcStateStore, SentState
from iotbot.devices.model import DeviceType, Registry

logger = logging.getLogger(__name__)

FEATURES = ("power_on", "power_off", "powerful")


class AcService:
    def __init__(self, registry: Registry, store: AcStateStore):
        self.store = store
        self.presets: dict[str, AcState] = {}
        self.warnings: list[str] = []
        self._locks: dict[str, asyncio.Lock] = {}
        for d in registry.devices.values():
            if d.kind != "ir" or d.device_type is not DeviceType.AIRCON or d.brand.lower() != "mitsubishi":
                continue
            on = d.features.get("power_on")
            if on is None or not on.codes:
                continue
            try:
                frames = pulses_to_frames(packet_to_pulses(on.codes[0]))
                if len(frames) != 2 or frames[0] != frames[1]:
                    raise AcFrameError(f"expected the same frame twice, got {len(frames)} frame(s)")
                preset = decode(frames[0])
            except AcFrameError as e:
                self.warnings.append(f"{d.id}: captured power_on is not a frame the AC encoder understands "
                                     f"({e}); sending captured codes")
                continue
            if not preset.power or preset.powerful:
                self.warnings.append(f"{d.id}: captured power_on is '{preset.describe()}', not a plain On; "
                                     "sending captured codes")
                continue
            self.presets[d.id] = preset

    def manages(self, device_id: str, feature: str | None = None) -> bool:
        return device_id in self.presets and (feature is None or feature in FEATURES)

    def lock(self, device_id: str) -> asyncio.Lock:
        """Held across build + send + save, so saved state follows send order."""
        return self._locks.setdefault(device_id, asyncio.Lock())

    def last(self, device_id: str) -> SentState | None:
        return self.store.get(device_id)

    def state_for(self, device_id: str, feature: str) -> AcState:
        """On = room preset; Powerful = preset + powerful with fan auto (as the remote
        sends it); Off = last sent state with power and powerful off."""
        preset = self.presets[device_id]
        if feature == "power_on":
            return preset
        if feature == "powerful":
            return preset.with_changes(fan="auto", powerful=True)
        if feature == "power_off":
            last = self.store.get(device_id)
            return (last.state if last else preset).with_changes(power=False, powerful=False)
        raise ValueError(f"no AC state for feature '{feature}'")

    @staticmethod
    def packet(state: AcState) -> bytes:
        return build_packet(state)

    async def remember(self, device_id: str, state: AcState, actor: str) -> str | None:
        """Save the sent state. The command already reached the remote, so a failed
        save is a warning, never a failure."""
        try:
            await self.store.put(device_id, state, actor)
        except Exception as e:  # noqa: BLE001
            logger.exception("Could not save AC state for %s", device_id)
            return f"sent, but could not save the AC state ({e.__class__.__name__})"
        return None
