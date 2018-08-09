
class DeviceException(Exception):
    """BaseDeviceException"""
    pass


class CommandNotFound(DeviceException):
    """Command not found for device"""
    pass


class DeviceTypeNotFound(DeviceException):
    pass


class BrandNotFound(DeviceException):
    pass


class AirconException(DeviceException):
    pass
