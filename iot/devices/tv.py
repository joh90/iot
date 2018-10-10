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
    LG = "lg"
    PANASONIC = "panasonic"
    PHILIPS = "philips"
    SAMSUNG = "samsung"
    SHARP = "sharp"
    SONY = "sony"
    TOSHIBA = "toshiba"
    VIZIO = "vizio"


class TVFactory:

    __slots__ = ("mappings",)

    def __init__(self):
        self.mappings = {}
        self.populate_mappings()

    def populate_mappings(self):
        # TODO: Check if brand's class exist
        # if not just create default BaseTV
        # class, in special cases we may need to overwrite or
        # add certain methods for an tv brand

        # Dynamically create tv brand classes
        for brand in TVBrands:
            kls = type(brand.value, (BaseTV,), {})
            self.mappings[brand.value] = kls

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
        # TODO: Take from Set Top Box, or migrate
        # code from set top box to multimedia device base class
        raise NotImplementedError
