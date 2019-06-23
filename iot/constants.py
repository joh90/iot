
ON_OFF = ("on", "off")

START_MESSAGE = '''
*Welcome to {}!*

List of possible commands:
/ping - returns pong and your user info
/status - returns server information
/list - returns the list of rooms and devices commands
/keyboard <room / device> - returns keyboard access to room / device commands
/on <device> - Turns on the targeted device
/off <device> - Turns on the targeted device
/d <device> <feature> <action or empty> (eg. `/d aircon temp up`, `/d tv mute`)
/user - returns list of approved users with delete option
/adduser - Add user conversation (provide both user id and username)
'''

PONG_MESSAGE = '''
PONG
Hi *{}*, your user id is {}
'''

STATUS_MESSAGE = '''
Server Time: {}
Server Uptime: {}
Server last handled command: {}

Broadlink Devices:
{}

Room(s): {}
Device(s): {}

Approved Users: {}
'''

ROOM_LIST_MESSAGE = '''
{}
====================================
RM Device:
{}

Registered Device(s):
{}

Registered Broadlink Device(s):
{}
'''

NO_ROOM_MESSAGE = 'Please add your room(s) and device(s) to *{}*'


LIST_MESSAGE = '''
Rooms:
{}
'''

# LIST_MESSAGE = '''
# Rooms:
# {}
# ====================================
# Scheduled Jobs:
# {}
# '''

DEVICE_NOT_FOUND = '''
Device *{}* not found, /list to list the device id and try again
'''

ROOM_OR_DEVICE_NOT_FOUND = '''
Room / Device *{}* not found, /list and try again
'''

ARGS_ERROR = '''
Parameters invalid, please try again
'''

DEVICE_FEATURE_ACTION_NOT_FOUND = '''
Device *{}*'s feature or action not found, /list to list the device's feature / action and try again
'''

DEVICE_COMMAND_NOT_IMPLEMENTED = '''
Device *{}* *{}* *{}* command not implemented
'''

SEND_DEVICE_COMMAND_ERROR = '''
Device {} {}, Error: {}
'''

USER_NOT_ALLOWED = '''
Please request permission from admin
'''
