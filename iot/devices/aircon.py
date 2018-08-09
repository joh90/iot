from enum import Enum

from iot.devices import (
    DeviceType, BaseDevice
)
from iot.devices.errors import (
    AirconException, BrandNotFound
)


class AirconBrands(Enum):
    DAIKIN = "daikin"


class AirconFactory:

    def __init__(self):
        self.mappings = {
            AirconBrands.DAIKIN.value: Daikin
        }

    def get_brand(self, brand):
        return self.mappings.get(brand.lower(), None)

    def create(self, room, id, brand, model):
        kls = self.get_brand(brand)

        if kls is None:
            raise BrandNotFound()

        aircon = kls(room, id, brand, model)

        return aircon


class BaseAircon(BaseDevice):

    device_type = DeviceType.AIRCON

    def set_powerful(self):
        raise NotImplementedError

    def temp_up(self):
        raise NotImplementedError

    def temp_down(self):
        raise NotImplementedError

    def toggle_mode(self):
        raise NotImplementedError

    def toggle_fan(self):
        raise NotImplementedError

    def toggle_swing(self):
        raise NotImplementedError


class Daikin(BaseAircon):
    def set_powerful(self):
        print("haha sneidng powerrrr")
        key = "powerful"
        self.set_action(key)
