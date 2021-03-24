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

    # TODO: implement menu -> up/down/left/right buttons
    # def toggle_menu(self):
    #     pass

    # def escape(self):
    #     pass

    # def enter(self):
    #     pass

    # def back(self):
    #     pass

    # def up(self):
    #     pass

    # def down(self):
    #     pass

    # def left(self):
    #     pass

    # def right(self):
    #     pass


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

    def toggle_menu(self):
        key = "toggle_menu"
        self.fire_action(key)

    def escape(self):
        key = "escape"
        self.fire_action(key)

    def enter(self):
        key = "enter"
        self.fire_action(key)

    def back(self):
        key = "back"
        self.fire_action(key)

    def up(self):
        key = "up"
        self.fire_action(key)

    def down(self):
        key = "down"
        self.fire_action(key)

    def left(self):
        key = "left"
        self.fire_action(key)

    def right(self):
        key = "right"
        self.fire_action(key)
