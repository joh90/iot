from iot.devices.errors import DeviceTypeNotFound
from iot.devices.factory import DeviceFactory


d_factory = DeviceFactory()


# We assume one blackbean per room for now
class Room:

    DEVICES = {}

    def __init__(self, name, blackbean):
        self.name = name
        self.blackbean = blackbean

    def room_info(self):
        return {
            "name": self.name,
            "host": self.blackbean.host,
            "mac": ''.join(format(x, '02x') for x in reversed(self.blackbean.mac)),
            "type": self.blackbean.type,
            "devices": self.DEVICES
        }

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
        print("sending data to rm2", data)
        self.blackbean.send_data(
            self.convert_to_bytearray(data)
        )
