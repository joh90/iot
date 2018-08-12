from enum import Enum

from iot.devices import (
    DeviceType, BaseDevice
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


class BaseTV(BaseDevice):

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
        raise NotImplementedError

    def channel_up(self):
        raise NotImplementedError

    def channel_down(self):
        raise NotImplementedError

    def volume_up(self):
        raise NotImplementedError

    def volume_down(self):
        raise NotImplementedError


class Panasonic(BaseTV):
    pass
