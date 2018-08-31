from iot.devices.errors import (
    DeviceException, CommandNotFound,
    BrandNotFound
)


class BaseDeviceKeyboardInterface:
    def power_on(self):
        raise NotImplementedError

    def power_off(self):
        raise NotImplementedError


class BaseDevice(BaseDeviceKeyboardInterface):
    """
    All command methods will return payload for blackbean to send

    `room` - Room object
    `id` - User-define name
    `brand` - Brand of device
    `model` - Model number of the device
    """

    __slots__ = (
        "device_type", "room", "id", "brand", "model",
        "commands", "last_action"
    )

    def __init__(self, room, id, brand, model):
        self.room = room

        self.id = id
        self.brand = brand
        self.model = model
        self.commmands = {}
        self.last_action = None

    def populate_device_commands(self, commands):
        self.commands = commands

    def update_last_action(self, action):
        device_action = ' '.join((self.id, action,))
        self.last_action = device_action
        self.room.last_action = device_action

    def get_command(self, action):
        if action not in self.commands:
            raise CommandNotFound

        return self.commands.get(action)

    def power_on(self):
        key = "power_on"
        self.set_action(key)

    def power_off(self):
        key = "power_off"
        self.set_action(key)

    def set_action(self, action):
        command = self.get_command(action)
        if command:
            self.room.send(command)
            self.update_last_action(action)

    def return_state(self):
        raise NotImplementedError
