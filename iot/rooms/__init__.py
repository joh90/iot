import logging

from iot.constants import ROOM_LIST_MESSAGE
from iot.utils import return_mac

from iot.devices import DeviceType
from iot.devices.broadlink import (
    BroadlinkDeviceFactory,
    BroadlinkDeviceTypes
)
from iot.devices.errors import (
    DeviceTypeNotFound, BrandNotFound,
    SendCommandError
)
from iot.devices.factory import DeviceFactory


logger = logging.getLogger(__name__)

d_factory = DeviceFactory()
bl_d_factory = BroadlinkDeviceFactory()


# We assume one RM3 RM per room for now
# Supports multiple Broadlink devices
# eg. Smart Plug, Multi Plugs
class Room:

    __slots__ = (
        "name",
        "rm",
        "DEVICES",
        "BL_DEVICES",
        "last_action"
    )

    def __init__(self, name, rm):
        self.name = name
        self.rm = rm
        self.DEVICES = {}
        self.BL_DEVICES = {}
        self.last_action = None

    def room_info(self):
        return {
            "name": self.name,
            "rm_host": self.rm.host[0] if self.rm else None,
            "rm_mac": return_mac(self.rm.mac) if self.rm else None,
            "type": self.rm.type if self.rm else None,
            "devices": self.DEVICES
        }

    def format_room_devices(self):
        room_devices = [
            "*{}* | Type: {}".format(d.id, DeviceType(d.device_type).name) \
            for d in self.DEVICES
        ]

        return room_devices

    def format_room_bl_devices(self):
        room_bl_devices = [
            "*{}* | Type: {}".format(d.id, d.device_type) \
            for d in self.BL_DEVICES.values()
        ]

        return room_bl_devices

    def room_list_info(self):
        info = self.room_info()

        room_devices = self.format_room_devices()
        room_broadlink_devices = self.format_room_bl_devices()

        return ROOM_LIST_MESSAGE.format(
            info["name"],
            "Type: {}, IP: {}, Mac: {}".format(
                info["type"], info["rm_host"], info["rm_mac"]),
            "\n".join(room_devices),
            "\n".join(room_broadlink_devices)
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

    def populate_broadlink_devices(self, devices):
        from iot.server import iot_server

        #populated = []

        for d in devices:
            if d["id"] not in self.BL_DEVICES:
                bl_device = iot_server.find_broadlink_device(
                    d["mac_address"], d["broadlink_type"].upper()
                )
                if bl_device is None:
                    logger.error(
                        "Room: %s, Unable to populate Broadlink device %s, " \
                        "Broadlink device not found for Device Type %s",
                        self.name, d["id"], d["broadlink_type"]
                    )
                    continue

                try:
                    dev = bl_d_factory.create_device(
                        d["broadlink_type"], self, d["id"], bl_device
                    )
                    self.add_broadlink_devices(dev.id, dev)
                    iot_server.devices[dev.id] = dev
                    print(self.BL_DEVICES)
                except DeviceTypeNotFound:
                    continue

    def add_broadlink_devices(self, id, bl_device):
        self.BL_DEVICES[id] = bl_device

    def convert_to_bytearray(self, data):
        return bytearray.fromhex("".join(data))

    def send(self, data):
        # Check device type
        if self.rm and self.rm.type == "RM2":
            self.send_rm_data(data)

    def send_rm_data(self, data):
        try:
            self.rm.send_data(
                self.convert_to_bytearray(data)
            )
        except Exception as e:
            raise SendCommandError("{}: {}".format(e.__class__, e))
