

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
/listusers
/adduser <user_name> <user_code>
/removeuser <user_name> <user_code>
/listtriggers
/newtrigger
/updatetrigger
/deletetrigger
/listhouse
/switchhouse - prompts house?
'''

DEVICE_NOT_FOUND = '''
Device *{}* not found, _/list_ to list the device id and try again
'''
