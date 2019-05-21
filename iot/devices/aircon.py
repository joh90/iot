from enum import Enum

from iot.devices import DeviceType
from iot.devices.base import (
    BaseDeviceKeyboardInterface, BaseDevice
)
from iot.devices.errors import (
    BrandNotFound
)


class AirconBrands(Enum):
    DAIKIN = "daikin"
    FUJITSU = "fujitsu"
    LG = "lg"
    MITSUBISHI = "mitsubishi"
    PANASONIC = "panasonic"
    SAMSUNG = "samsung"
    SANYO = "sanyo"
    TOSHIBA = "toshiba"


class AirconFactory:

    __slots__ = ("mappings",)

    def __init__(self):
        self.mappings = {}
        self.populate_mappings()

    def populate_mappings(self):
        # TODO: Check if model's class exist,
        # if not just create default BaseAircon
        # class, in special cases we may need to overwrite or
        # add certain methods for an aircon brand or
        # at model level
        # Refer to http://www.diveintopython.net/file_handling/more_on_modules.html

        # As of now, we can manually create the mapping,
        # using a different brand name, see projector for example

        # Dynamically create aircon brand classes
        for brand in AirconBrands:
            kls = type(brand.value, (BaseAircon,), {})
            self.mappings[brand.value] = kls

    def get_brand(self, brand):
        return self.mappings.get(brand.lower(), None)

    def create(self, room, id, brand, model):
        kls = self.get_brand(brand)

        if kls is None:
            raise BrandNotFound

        aircon = kls(room, id, brand, model)

        return aircon


class AirconKeyboardInterface(BaseDeviceKeyboardInterface):

    def power_on_high(self):
        pass

    def power_on_low(self):
        pass

    def power_on_dry(self):
        pass

    def powerful(self):
        pass

    def temp_up(self):
        pass

    def temp_down(self):
        pass

    def toggle_mode(self):
        pass

    def toggle_fan(self):
        pass

    def toggle_swing(self):
        pass


class BaseAircon(BaseDevice, AirconKeyboardInterface):

    device_type = DeviceType.AIRCON

    def power_on_high(self):
        key = "power_on_high"
        self.fire_action(key)

    def power_on_low(self):
        key = "power_on_low"
        self.fire_action(key)

    def power_on_dry(self):
        key = "power_on_dry"
        self.fire_action(key)

    def powerful(self):
        key = "powerful"
        self.fire_action(key)

    def temp_up(self):
        raise NotImplementedError

    def temp_down(self):
        raise NotImplementedError

    def toggle_mode(self):
        raise NotImplementedError

    def toggle_fan(self):
        key = "toggle_fan"
        self.fire_action(key)

    def toggle_swing(self):
        key = "toggle_swing"
        self.fire_action(key)
