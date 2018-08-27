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


class SetTopBoxKeyboardInterface(MultimediaKeyboardInterface):
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


class BaseSetTopBox(MultimediaDevice, SetTopBoxKeyboardInterface):

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


class Samsung(BaseSetTopBox):
    pass
