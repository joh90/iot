import sys
import time
from enum import Enum

from iot.devices import DeviceType
from iot.devices.base.multimedia import (
    MultimediaKeyboardInterface,
    MultimediaDevice
)
from iot.devices.errors import (
    BrandNotFound
)


class ProjectorBrands(Enum):
    BARCO = "barco"
    BENQ = "benq"
    CANON = "canon"
    CASIO = "casio"
    EPSON = "epson"
    HITACHI = "hitachi"
    INFOCUS = "infocus"
    JVC = "jvc"
    LG = "lg"
    NEC = "nec"
    PANASONIC = "panasonic"
    PROXIMA = "proxima"
    SONY = "sony"
    VIEWSONIC = "viewsonic"
    VIVITEK = "vivitek"


class ProjectorFactory:

    __slots__ = ("mappings",)

    def __init__(self):
        self.mappings = {}
        self.populate_mappings()

    def populate_mappings(self):
        # TODO: Check if model's class exist
        # if not just create default BaseProjector
        # class, in special cases we may need to overwrite or
        # add certain methods for an projector brand/model

        # Dynamically create tv brand classes
        for brand in ProjectorBrands:
            kls = type(brand.value, (BaseProjector,), {})
            self.mappings[brand.value] = kls

        self.mappings["epsoneb"] = EpsonEB

    def get_brand(self, brand):
        return self.mappings.get(brand.lower(), None)

    def create(self, room, id, brand, model):
        kls = self.get_brand(brand)

        if kls is None:
            raise BrandNotFound

        tv = kls(room, id, brand, model)

        return tv


class ProjectorKeyboardInterface(MultimediaKeyboardInterface):

    def change_input(self):
        pass

    def hdmi(self):
        pass

    def computer(self):
        pass

    # def zoom_in(self):
    #     pass

    # def zoom_out(self):
    #     pass

    def freeze(self):
        pass


class BaseProjector(MultimediaDevice, ProjectorKeyboardInterface):

    device_type = DeviceType.PROJECTOR

    def change_input(self):
        key = "change_input"
        self.fire_action(key)

    def hdmi(self):
        key = "hdmi"
        self.fire_action(key)

    def computer(self):
        key = "computer"
        self.fire_action(key)

    def zoom_in(self):
        key = "zoom_in"
        self.fire_action(key)

    def zoom_out(self):
        key = "zoom_out"
        self.fire_action(key)

    def freeze(self):
        key = "freeze"
        self.fire_action(key)


class EpsonEB(BaseProjector):

    def standby(self):
        key = "standby"
        self.fire_action(key)

    def search(self):
        key = "search"
        self.fire_action(key)

    def power_off(self):
        """
        To turn off, projector needs to be in "standby" prompt,
        then send another "standby" command to turn it off
        """
        command = self.get_command("standby")

        for i in range(2):
            self.room.send(command)

            # Introduce delay so that the IR receiver can work
            time.sleep(2)
