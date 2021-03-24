import logging
import time

from enum import Enum

from iot.devices import DeviceType
from iot.devices.base.multimedia import (
    MultimediaKeyboardInterface,
    MultimediaDevice
)
from iot.devices.errors import (
    CommandNotFound, InvalidArgument,
    BrandNotFound
)


logger = logging.getLogger(__name__)


class AmplifierBrands(Enum):
    CITYPULSE = "citypulse"
    MARANTZ = "marantz"


class AmplifierFactory:

    __slots__ = ("mappings",)

    def __init__(self):
        self.mappings = {}
        self.populate_mappings()

    def populate_mappings(self):
        # TODO: Check if brand's class exist
        # if not just create default BaseAmplifier
        # class, in special cases we may need to overwrite or
        # add certain methods for an tv brand

        # Dynamically create tv brand classes
        for brand in AmplifierBrands:
            kls = type(brand.value, (BaseAmplfiier,), {})
            self.mappings[brand.value] = kls

    def get_brand(self, brand):
        return self.mappings.get(brand.lower(), None)

    def create(self, room, id, brand, model):
        kls = self.get_brand(brand)

        if kls is None:
            raise BrandNotFound

        amp = kls(room, id, brand, model)

        return amp


class AmplifierKeyboardInterface(MultimediaKeyboardInterface):

    def change_input(self):
        pass

    # def cd(self):
    #     pass

    def network(self):
        pass

    def usb(self):
        pass

    def aux(self):
        pass

    def optical(self):
        pass

    def coax(self):
        pass


class BaseAmplfiier(MultimediaDevice, AmplifierKeyboardInterface):

    device_type = DeviceType.AMPLIFIER

    def change_input(self):
        key = "input"
        self.fire_action(key)

    def network(self):
        key = "network"
        self.fire_action(key)

    def usb(self):
        key = "usb"
        self.fire_action(key)

    def aux(self):
        key = "aux"
        self.fire_action(key)

    def optical(self):
        key = "optical"
        self.fire_action(key)

    def coax(self):
        key = "coax"
        self.fire_action(key)
