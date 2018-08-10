

START_MESSAGE = '''
*Welcome to JoH IOT!*

List of possible commands:
/ping - returns pong and current house selected
/status - returns list of BL devices connected
/keyboard <room> - returns keyboard access to rooms / devices and commands
/list - returns the list of rooms and devices commands
/on <device> - Turns on the targeted device
/off <device> - Turns on the targeted device

Future:
/d <device> temp <number>
/d <device> volume <up/down>
/d <device> <option> <action>
/c <custom-action>
/listusers
/adduser <user-name> <user-code>
/removeuser <user-name> <user-code>
/listtriggers
/newtrigger
/updatetrigger
/deletetrigger
/listhouse
/switchhouse - prompts house?
'''

PONG_MESSAGE = '''
PONG
Hi {}, your user_id is {}
'''

DEVICE_NOT_FOUND = '''
Device *{}* not found, /list to list the device id and try again
'''

USER_NOT_ALLOWED = '''
Please request permission from admin
'''
