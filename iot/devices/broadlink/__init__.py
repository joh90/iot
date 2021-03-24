from enum import Enum

from iot.devices.base import (
    BaseDeviceKeyboardInterface,
    BaseBroadlinkDevice
)
from iot.devices.errors import (
    DeviceTypeNotFound
)


class BroadlinkDeviceFactory:

    __slots__ = ("device_mappings", "interface_mappings",)

    def __init__(self):
        self.device_mappings = {}
        self.interface_mappings = {
            BroadlinkDeviceTypes.SP2.name: SP2BroadlinkDeviceKeyboardInterface,
            BroadlinkDeviceTypes.SP4.name: SP4BroadlinkDeviceKeyboardInterface,
        }
        self.populate_mappings()

    def populate_mappings(self):
        for device_type in BroadlinkDeviceTypes:
            self.device_mappings[device_type.name] = device_type.value


    def get_device_type_interface(self, device_type):
        class_interface = self.interface_mappings.get(device_type)
        if class_interface:
            return [
                i for i in dir(class_interface) \
                if not i.startswith('__')
            ]

    def get_device_type(self, device_type):
        return self.device_mappings.get(device_type, None)

    def create_device(self, device_type, room, id, bl_device):
        kls = self.get_device_type(device_type)

        if kls is None:
            logger.error(
                "Unable to create Broadlink device type %s, " \
                "Room %s, id %s",
                device_type, room.name, id
            )
            raise DeviceTypeNotFound

        broadlink_device = kls(room, id, bl_device)

        return broadlink_device


class SP2BroadlinkDeviceKeyboardInterface(BaseDeviceKeyboardInterface):

    def check_power_state(self):
        pass


class SP4BroadlinkDeviceKeyboardInterface(SP2BroadlinkDeviceKeyboardInterface):

    def check_nightlight_state(self):
        pass

    def nightlight_on(self):
        pass

    def nightlight_off(self):
        pass


class SP2BroadlinkDevice(BaseBroadlinkDevice):

    def power_on(self):
        self.bl_device.set_power(True)
        result, _ = self.check_power_state()

        if result is True:
            return True, "Turned ON {}".format(self.id)

    def power_off(self):
        self.bl_device.set_power(False)
        result, _ = self.check_power_state()

        if result is False:
            return False, "Turned OFF {}".format(self.id)

    def check_power_state(self):
        result = self.bl_device.check_power()

        if isinstance(result, bool):
            state = "ON" if result else "OFF"
            return result, "{} is {}".format(self.id, state)

    def get_energy(self):
        raise NotImplementedError


class SP4BroadlinkDevice(SP2BroadlinkDevice):

    def nightlight_on(self):
        self.bl_device.set_nightlight(True)
        result, _ = self.check_nightlight_state()

        if result is True:
            return True, "Turned ON {}".format(self.id)

    def nightlight_off(self):
        self.bl_device.set_nightlight(False)
        result, _ = self.check_nightlight_state()

        if result is False:
            return False, "Turned OFF {}".format(self.id)

    def check_nightlight_state(self):
        result = self.bl_device.check_nightlight()

        if isinstance(result, bool):
            state = "ON" if result else "OFF"
            return result, "{} is {}".format(self.id, state)


class BroadlinkDeviceTypes(Enum):

    SP2 = type("SP2", (SP2BroadlinkDevice,), {})
    SP4 = type("SP4", (SP4BroadlinkDevice,), {})
