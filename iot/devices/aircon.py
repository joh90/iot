from iot.devices import DeviceType, BaseDevice


DAIKIN = "DAIKIN"


class BaseAircon(BaseDevice):

    device_type = DeviceType.AIRCON

    def set_powerful(self):
        raise NotImplementedError

    def temp_up(self):
        raise NotImplementedError

    def temp_down(self):
        raise NotImplementedError

    def toggle_mode(self):
        raise NotImplementedError

    def toggle_fan(self):
        raise NotImplementedError

    def toggle_swing(self):
        raise NotImplementedError


class Daikin(BaseAircon):
    def set_powerful(self):
        
