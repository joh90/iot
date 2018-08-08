from enum import Enum

import broadlink


## TODO: Make it a class able to return back a single class to call instead
def populate_devices():
    return broadlink.discover(timeout=5)


class Room(object):

    DEVICES = {}

    def __init__(self, blackbean):
        self.blackbean = blackbean

    def blackbean_info(self):
        return {
            "host": self.blackbean.host,
            "mac": ''.join(format(x, '02x') for x in reversed(self.blackbean.mac)),
            "type": self.blackbean.type
        }

    def add_device(self, device):
        pass

    def get_device(self, device_id):
        pass


class DeviceType(Enum):
    AIRCON = 1
    TV = 2
    SET_TOP_BOX = 3


class BaseDevice(object):
    """
    All command methods will return payload for blackbean to send

    `id` - <room>-<device_type>-<brand>-<model / location # eg. office-tv-pansonic-wall
    """

    OPTIONAL_COMMAND = {}

    device_type = None
    last_command = None

    def __init__(self, id, brand, model):
        pass

    def power_on(self):
        raise NotImplementedError

    def power_off(self):
        raise NotImplementedError

    def run_command(self, option):
        # TODO: Make exceptions special
        if option not in self.OPTIONAL_COMMAND:
            raise Exception()

    def return_state(self):
        raise NotImplementedError

    def last_command_issued(self):
        raise NotImplementedError
