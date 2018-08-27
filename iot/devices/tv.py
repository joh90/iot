from enum import Enum

from iot.devices import DeviceType
from iot.devices.base.multimedia import (
    MultimediaKeyboardInterface,
    MultimediaDevice
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


class TVKeyboardInterface(MultimediaKeyboardInterface):
    def change_input(self):
        pass


class BaseTV(MultimediaDevice, TVKeyboardInterface):

    device_type = DeviceType.TV

    def change_input(self):
        raise NotImplementedError

    def channel(self, chan_number):
        # TODO: Take from Set Top Box, ideally make a new generic mm class
        raise NotImplementedError


class Panasonic(BaseTV):
    pass
