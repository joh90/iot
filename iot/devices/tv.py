from iot.devices import BaseDevice


PANASONIC = "PANASONIC"


class Television(BaseDevice):

    DIGITS = []

    def change_input(self):
        raise NotImplementedError

    def set_mute(self):
        raise NotImplementedError

    def go_to_channel(self, chan_number):
        raise NotImplementedError

    def volume_up(self):
        raise NotImplementedError

    def volume_down(self):
        raise NotImplementedError

    def channel_up(self):
        raise NotImplementedError

    def channel_down(self):
        raise NotImplementedError
