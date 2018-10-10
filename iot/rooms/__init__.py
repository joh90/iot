import logging

from iot.constants import ROOM_LIST_MESSAGE
from iot.utils import return_mac

from iot.devices import DeviceType
from iot.devices.errors import (
    DeviceTypeNotFound, BrandNotFound,
    SendCommandError
)
from iot.devices.factory import DeviceFactory


logger = logging.getLogger(__name__)

d_factory = DeviceFactory()


# We assume one blackbean per room for now
class Room:

    __slots__ = ("name", "blackbean", "DEVICES", "last_action")

    def __init__(self, name, blackbean):
        self.name = name
        self.blackbean = blackbean
        self.DEVICES = {}
        self.last_action = None

    def room_info(self):
        return {
            "name": self.name,
            "host": self.blackbean.host,
            "mac": return_mac(self.blackbean.mac),
            "type": self.blackbean.type,
            "devices": self.DEVICES
        }

    def room_list_info(self):
        info = self.room_info()

        room_devices = [
            "*{}* | Type: {}".format(d.id, DeviceType(d.device_type).name) \
            for d in info["devices"].values()
        ]

        return ROOM_LIST_MESSAGE.format(
            info["name"],
            "Type: {}, IP: {}, Mac: {}".format(
                info["type"], info["host"][0], info["mac"]),
            "\n".join(room_devices)
        )


    def populate_devices(self, devices):
        populated = []

        for d in devices:
            if d["id"] not in self.DEVICES:
                try:
                    dev = d_factory.create_device(
                        d["type"], self, d["id"], d["brand"], d["model"]
                    )

                    self.add_device(dev)
                    populated.append(dev)
                except DeviceTypeNotFound:
                    continue
                except BrandNotFound:
                    logger.error(
                        "Room: %s, Unable to populate device %s, " \
                        "Brand %s not found for Device Type %s",
                        self.name, d["id"], d["brand"], d["type"]
                    )
                    continue

        return populated

    def add_device(self, device):
        self.DEVICES[device.id] = device

    def get_device(self, device_id):
        pass

    def convert_to_bytearray(self, data):
        return bytearray.fromhex("".join(data))

    def send(self, data):
        # Check device type
        if self.blackbean.type == "RM2":
            self.send_blackbean_data(data)

    def send_blackbean_data(self, data):
        try:
            self.blackbean.send_data(
                self.convert_to_bytearray(data)
            )
        except Exception as e:
            raise SendCommandError("{}: {}".format(e.__class__, e))
