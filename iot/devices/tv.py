from enum import Enum

from iot.devices import (
    DeviceType, BaseDeviceKeyboardInterface, BaseDevice
)
from iot.devices.errors import (
    BrandNotFound
)


class TVBrands(Enum):
    PANASONIC = "panasonic"


class TVFactory:

    def __init__(self):
        self.mappings = {
            TVBrands.PANASONIC.value: Panasonic
        }

    def get_brand(self, brand):
        return self.mappings.get(brand.lower(), None)

    def create(self, room, id, brand, model):
        kls = self.get_brand(brand)

        if kls is None:
            raise BrandNotFound

        tv = kls(room, id, brand, model)

        return tv


class TVKeyboardInterface(BaseDeviceKeyboardInterface):
    def change_input(self):
        pass

    def mute(self):
        pass

    def unmute(self):
        pass

    def channel_up(self):
        pass

    def channel_down(self):
        pass

    def volume_up(self):
        pass

    def volume_down(self):
        pass


class BaseTV(BaseDevice, TVKeyboardInterface):

    device_type = DeviceType.TV

    def change_input(self):
        raise NotImplementedError

    def mute(self):
        key = "mute"
        self.set_action(key)

    def unmute(self):
        key = "mute"
        self.set_action(key)

    def channel(self, chan_number):
        # TODO: Take from Set Top Box, ideally make a new generic mm class
        raise NotImplementedError

    def channel_up(self):
        key = "channel_up"
        self.set_action(key)

    def channel_down(self):
        key = "channel_down"
        self.set_action(key)

    def volume_up(self):
        key = "volume_up"
        self.set_action(key)

    def volume_down(self):
        key = "volume_down"
        self.set_action(key)


class Panasonic(BaseTV):
    pass
