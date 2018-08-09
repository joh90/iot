from iot.devices import DeviceType
from iot.devices.aircon import AirconFactory
from iot.devices.errors import DeviceTypeNotFound


class DeviceFactory:
    def __init__(self):
        self.device_mapping = {
            DeviceType.AIRCON: AirconFactory()
        }

    def get_device_type_factory(self, device_type):
        return self.device_mapping.get(device_type, None)

    def create_device(self, device_type, room, id, brand, model):
        kls_factory = self.get_device_type_factory(device_type)

        if kls_factory is None:
            raise DeviceTypeNotFound()

        device = kls_factory.create(room, id, brand, model)

        return device
