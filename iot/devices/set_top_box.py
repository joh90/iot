import logging
import time

from enum import Enum

from iot.devices import (
    DeviceType, BaseDevice
)
from iot.devices.errors import (
    CommandNotFound, InvalidArgument,
    BrandNotFound
)


logger = logging.getLogger(__name__)


class SetTopBoxBrands(Enum):
    SAMSUNG = "samsung"


class SetTopBoxFactory:

    def __init__(self):
        self.mappings = {
            SetTopBoxBrands.SAMSUNG.value: Samsung
        }

    def get_brand(self, brand):
        return self.mappings.get(brand.lower(), None)

    def create(self, room, id, brand, model):
        kls = self.get_brand(brand)

        if kls is None:
            raise BrandNotFound

        stb = kls(room, id, brand, model)

        return stb


class BaseSetTopBox(BaseDevice):

    # TODO: Network provider channel mappings
    # Maybe curl from this to create mapping?
    # https://www.tvchannellists.com/Main_Page
    # Should ideally do it another class too

    device_type = DeviceType.SET_TOP_BOX

    def get_digit(self, digit):
        digits = self.get_command("digits")

        # If not all digits (0-9) (10 numbers) are populated
        # We will raise error
        if not digits or len(digits) != 10:
            raise CommandNotFound

        return digits[digit]

    def mute(self):
        key = "mute"
        self.set_action(key)

    def unmute(self):
        key = "mute"
        self.set_action(key)

    def channel(self, chan_number):
        """Experimental function, may not work in some cases"""
        cmd_to_send = []

        for digit in list(chan_number):
            try:
                d = int(digit)
                command_digit = self.get_digit(d)
                cmd_to_send.append(command_digit)
            except ValueError:
                logger.error("Unable to convert digit to string, %s", digit)
                raise InvalidArgument
            except CommandNotFound:
                raise

        if len(cmd_to_send) > 0:
            for cmd in cmd_to_send:
                self.room.send(cmd)
                # Introduce delay so that the IR receiver can work
                time.sleep(2.25)
        else:
            raise InvalidArgument

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


class Samsung(BaseSetTopBox):
    pass
