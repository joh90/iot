import logging

from iot.devices import DeviceType
from iot.devices.aircon import AirconFactory, AirconKeyboardInterface
from iot.devices.errors import DeviceTypeNotFound
from iot.devices.set_top_box import SetTopBoxFactory, SetTopBoxKeyboardInterface
from iot.devices.tv import TVFactory, TVKeyboardInterface


logger = logging.getLogger(__name__)


# TODO: create a generic device type factory
class DeviceFactory:
    def __init__(self):
        self.device_mapping = {
            DeviceType.AIRCON: AirconFactory(),
            DeviceType.TV: TVFactory(),
            DeviceType.SET_TOP_BOX: SetTopBoxFactory()
        }

        self.interface_mapping = {
            DeviceType.AIRCON: AirconKeyboardInterface,
            DeviceType.TV: TVKeyboardInterface,
            DeviceType.SET_TOP_BOX: SetTopBoxKeyboardInterface,
        }

    def get_device_type_factory(self, device_type):
        return self.device_mapping.get(device_type, None)

    def get_device_type_interface(self, device_type):
        class_interface = self.interface_mapping.get(device_type)
        if class_interface:
            return [
                i for i in dir(class_interface) \
                if not i.startswith('__')
            ]

    def create_device(self, device_type, room, id, brand, model):
        kls_factory = self.get_device_type_factory(device_type)

        if kls_factory is None:
            logger.error(
                "Unable to create device type %s, " \
                "Room %s, id %s, Brand %s, Model %s",
                DeviceType(device_type), room.name, id, brand, model
            )
            raise DeviceTypeNotFound

        device = kls_factory.create(room, id, brand, model)

        return device
