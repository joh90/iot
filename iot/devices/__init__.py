from enum import IntEnum

import broadlink

from iot.devices.errors import (
    DeviceException, CommandNotFound,
    BrandNotFound
)


## TODO: Make it a class able to return back a single class to call instead
def populate_devices():
    return broadlink.discover(timeout=5)


class DeviceType(IntEnum):
    AIRCON = 1
    TV = 2
    SET_TOP_BOX = 3


class BaseDevice:
    """
    All command methods will return payload for blackbean to send

    `room` - Room object
    `id` - User-define name
    """

    commands = {}

    # Custom multi commands
    optional_commands = {}

    device_type = None
    last_command = None

    def __init__(self, room, id, brand, model):
        self.room = room

        self.id = id
        self.brand = brand
        self.model = model

        #self.populate_device_command()

    def populate_device_commands(self, commands):
        self.commands = commands

    def get_command(self, action):
        if action not in self.commands:
            raise CommandNotFound()

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

    def return_state(self):
        raise NotImplementedError

    def last_command_issued(self):
        raise NotImplementedError
