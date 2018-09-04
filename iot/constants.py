

ON_OFF = ("on", "off")

START_MESSAGE = '''
*Welcome to JoH IOT!*

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

Future:
/cm <custom-macro>
/listtriggers
/newtrigger
/updatetrigger
/deletetrigger
/listhouse (hard, must be in same network)
/switchhouse - prompts house? (hard, must be in same network)
'''

PONG_MESSAGE = '''
PONG
Hi *{}*, your user id is {}
'''

STATUS_MESSAGE = '''
Server Time: {}
Server Uptime: {}
Server last handled command: {}

Blackbean Devices:
{}

Room(s): {}
Device(s): {}

Approved Users: {}

Future:
Room's Last command handled
Version
'''

ROOM_LIST_MESSAGE = '''
{}
====================================
Blackbean:
{}
{}
'''

LIST_MESSAGE = '''
Rooms:
{}
'''

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

USER_NOT_ALLOWED = '''
Please request permission from admin
'''
