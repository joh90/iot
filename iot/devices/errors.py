# TODO: Write docstring for all errors

class DeviceException(Exception):
    """BaseDeviceException"""
    pass


class CommandNotFound(DeviceException):
    """Command not found for device"""
    pass


class BroadlinkDeviceRequired(Exception):
    """BaseBroadlinkDeviceRequire error"""
    pass


class InvalidArgument(DeviceException):
    pass


class DeviceTypeNotFound(DeviceException):
    pass


class BrandNotFound(DeviceException):
    pass


class SendCommandError(DeviceException):
    pass


class AirconException(DeviceException):
    pass
