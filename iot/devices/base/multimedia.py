from iot.devices.base import BaseDevice, BaseDeviceKeyboardInterface


class MultimediaKeyboardInterface(BaseDeviceKeyboardInterface):
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


class MultimediaDevice(BaseDevice, MultimediaKeyboardInterface):

    def mute(self):
        key = "mute"
        self.fire_action(key)

    def unmute(self):
        key = "mute"
        self.fire_action(key)

    def channel_up(self):
        key = "channel_up"
        self.fire_action(key)

    def channel_down(self):
        key = "channel_down"
        self.fire_action(key)

    def volume_up(self):
        key = "volume_up"
        self.fire_action(key)

    def volume_down(self):
        key = "volume_down"
        self.fire_action(key)
