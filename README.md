# Telegram IOT Bot
Python / Broadlink powered Telegram bot
Control your home/office devices with Telegram


# Table of contents
- [Introduction](#introduction)
- [Requirements](#requirements)
- [Getting Started](#getting-started)
	* [BotFather](#botfather)
	* [Blackbean Device](#blackbean-devices)
	  * [RM3](#rm3)
	* [Rooms, Devices & Commands](#rooms-devices-commands)
	  * [Rooms](#rooms)
	  * [Devices](#devices)
	    * [Type Enums](#type-enums)
	  * [Device's Commands](#device-commands)
	* [Users](#users)
	* [Sample JSON](#sample-jsons)
	* [Learning Commands](#learning-commands)
	* [Usage](#usage)
	  * [Controlling Devices](#controlling-devices)
	  * [Add Users](#add-users)
	  * [Remove Users](#remove-users)
- [Running the application](#run)
- [Screenshots](#screenshots)
- [Videos](#videos)
- [Future features / Improvements](#future)
- [Project Limitation](#limitation)
- [Contributing](#contributing)
- [License](#license)


# Introduction
Control various devices such as (TV, Aircon, Set Top Box) with this Telegram Bot!


### Features
* Supports various household appliances
* Only approved users can use the bot
* In-line keyboard menu to control your devices
* Learn and add commands to JSON file for device's commands
* Add / Delete users
* Query server's last handled request / command


# Requirements
### Requirements
* Python 3.3+
* [Python Telegram Bot](https://python-telegram-bot.org/)
* [Python-Broadlink](https://github.com/mjg59/python-broadlink)
* Supported Broadlink Devices
	* [RM2](http://www.ibroadlink.com/rmMini3/)
	* [TC2](http://www.ibroadlink.com/tc2/)
	* [RMPro+](http://www.ibroadlink.com/rmPro+/)
	* etc.

### Optional
* [Raspberry PI](https://www.raspberrypi.org/products/)


# Getting Started
### BotFather
* Create new bot in Telegram
1. Find @BotFather in Telegram
1. Type `/help` for commands
1. Key in `/newbot` and save your bot id and secret
![Example snippet from Botfather](docs/screenshots/botfather_id_secret.jpg)

* Populate shortcut commands to your Telegram bot
![Shortcut command button](docs/screenshots/shortcut_command_button.jpg)
1. Find @BotFather in Telegram
1. Type `/setcommands` in Telegram
1. Paste the following text snippet to Telegram
```
start - list of general commands
ping - returns pong and your user info
status - returns server information
list - returns the list of rooms and devices commands
keyboard - returns keyboard access to rooms commands
user - returns list of approved users with delete option
adduser - Add user conversation (provide both user id and username)
```
4. Success!, Botfather will notify `Success! Command list updated. /help`
![After populating commands](docs/screenshots/shortcut_commands.jpg)

### Blackbean Devices
#### RM3
[Full Manual](http://download.appinthestore.com/201605/Broadlink%20IR%20Remote%20Controller%20Manual.pdf)

* Unbox Device
![RM3 unboxing](docs/screenshots/rm3.jpg =600x320)
1. USB Cable, RM3 Device
1. Connect RM3 device with power with USB cable

* Install Mobile App and configure Blackbean device
1. Install Broadlink's e-Control App [Link](http://www.ibroadlink.com/app/)
1. Press on `+` and then `Add device`
![Press on '+' and then 'Add device'](docs/screenshots/app_1.jpg =292x501)
1. Key in your Wifi's ssid and password and press `Configure`
![Configure Wifi](docs/screenshots/app_2.jpg =292x501)
1. When successful, your device will show up in device list with the MAC address(`+` -> Device list)
![Device list](docs/screenshots/app_3.jpg =292x501)

### Rooms Devices Commands
#### Rooms
* To add a room, use the following JSON format and add it to `devices.json` file
* Supports one device per room only
* Add the Blackbean device's MAC address that you are placing in the room
(To get device's IP or MAC address, refer to [Learning Commands](#learning-commands))
* MAC address value should be used from the device list from the app (without the colon `:`)

eg. Adding Office
```
{
	"office": {
		"mac_address": "780f771abcde",
		"blackbean_type": "RM2",
		"devices": []
	}
}
```

#### Devices
##### Type Enums
| Types  | Devices       |
| -------|:-------------:|
| 1      | Air-con       |
| 2      | TV            |
| 3      | Set Top Box   |

* Model is not used at the moment
* Add to room's `devices` list
* To add device(s) into a room, use the following JSON format

eg. Office's Aircon
```
{
	"type": 1,
	"id": "office-aircon",
	"brand": "daikin",
	"model": "super-multi-nx"
}
```

#### Device Commands
* Only Brand name is used, model is not used at the moment
* Key value of Command interface name -> Command String (from bytearray)
* All device's commands should have at least `power_on` and `power_off` key / value
* Contribute to the project by committing your device's commands (`commands.json`)

eg. Daikin Aircon commands
```
{
	"1": {
		"daikin": {
			"power_on": "26005002100e0e0e0f0e0e0e0f0e0e00034473380f2a0f0e0f0e0e0e0e2a0f0e0f0e0e0e0f0e0e2a0f0e0e2a0f2a0f0e0f290f2a0f2a0f2a0f2a0f0e0e0f0e2a0f0e0e0e0f0e0e0e0f0e0e0e0e0f0e0e0e0e0f0e0e2a0f0e0f290f0f0e0e0e0e0f291029100e0e0e0e0f0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f290f2a0f2a0f0e0f29100e0e2a0f2a0f00047b73380f2a0f0e0e0e0f0e0e2a0f0e0f0e0e0e0e0f0e2a0f0e0e2a0f2a0f0e0e2a0f2a0f2a0f2a0f2a0f0e0e0e0f29100e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e2a0f0e0f0e0e0e0f0e0e2a0f0e0e2a0f0e0f29102910290f0f0e0e0e0e0f29100e0e2a0f0e0e0e0f0e0e0e0f0e0e0e0f2910290f0e0f2910291029100e0e00047b73380f2a0f0e0f0e0e0e0e2a0f0e0f0e0e0e0f0e0e2a0f0e0e2b0e2a0f0e0f290f2a0f2a0f2a0f2a0f0e0e0f0e29100e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f29100e0e0e0f290f2a0f2a0f0e0f0e0e0e0f0e0e2a0f29100e0e2a0f0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e2a0f2a0f2a0f2a0f2a0f0e0e2a0f0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e2a0f2b0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0d0f0e0e2a0f2a0f0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0d0f0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e2a0f2a0f0e0f290f0e0f0e0e0e0f0e0e0e0f0e0e0e0e2a0f0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0d0f0e0f0e0e0e0f000d050000000000000000",
			"power_off": "26005002100d0f0d0f0d100d0f0d1000034473371029100d0f0d0f0d1029100d0f0d100d0f0d10290f0d10291029100d0f291029102910290f2a0f0d100d0f2a0f0d100d0f0d0f0d100d0f0d100d0f0d100d0f0d0f2a0f0d1029100d0f0d100d0f291029100d0f0d100d0f0d0f0e0f0d0f0d100d0f0d100d0f0d100d0f0d0f0d100d0f0d102910290f2a0f0d1029100d0f2a0f291000047b73371029100d0f0d100d0f29100d0f0d100d0f0d10290f0e0f291029100d0f2a0f291029102910290f0e0f0d0f2a0f0d100d0f0d100d0f0d0f0d100d0f0d100d0f0d100d0f29100d0f0d100d0f0d1029100d0f29100d10290f0d100d0f0d0f2a0f2a0f0d100d0f0d100d0f0d0f0d100d0f0d1029100d0f0d0f2a0f2a0f0d100d0f0d1000047a733810290f0d100d0f0d1029100d0f0d0f0d100d0f2a0f0d10290f2a0f0d1029102910290f2a0f2a0f0d100d0f29100d0f0d100d0f0d100d0f0d100d0f0d0f0d100d0f0d100d0f0d100d0f0d0f0e0f0d0f0d100d0f0d100d0f2910291029100d0f0d100d0f0d0f2a0f2a0f0d1029100d0f0d0f0d100d0f0d100d0f0d100d0f0d0f0d10291029102910290f2a0f0d1029100d0f0d0f0d100d0f0d100d0f0d100d0f0d0f0e0f0d0f0d100d0f0d100d0f0d100d0f0d0f2a0f2a0f0d100d0f0d100d0f0d0f0d100d0f0d100d0f0d10290f2a0f0d100d0f0d100d0f0d0f0e0f0d0f0d100d0f0d100d0f0d100d0f0d0f0d100d0f0d100d0f0d100d0f0d0f0d100d0f2a0f2a0f0d10290f0d100d0f0d100d0f0d100d0f0d0f2a0f0d100d0f0d100d0f0d100d0f291029102910290f2a0f2a0f2a0f2a0f000d050000000000000000",
			"powerful": "260050020f0e0f0e0e0e0f0e0e0e0e000345723810290f0e0f0e0e0e0f29100e0e0e0f0e0e0e0e2a0f0e0f291029100e0e2a0f291029102910290f0e0f0e0e2a0f0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f29100d0f29100e0e0e0f0e0e2a0f2a0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e2a0f291029100e0e2a0f0e0e2a0f2b0e00047b73380f2a0f0e0f0e0e0e0f2a0f0e0e0e0e0e0f0e0e2a0f0e0f2910290f0e0f291029102910290f2a0f0e0f0e0e2a0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f29100e0e0e0f0e0e0e0f290f0e0f0e0e2a0f2a0f2a0f2a0f0e0e0e0f0e0e2a0f0e0f290f0e0f0e0e0e0f0e0e0e0f2910290f2a0f0e0f29102910290f0e0f00047b73380f2a0f0e0e0e0f0e0e2a0f0e0f0e0e0e0e0e0f29100e0e2a0f2a0f0e0e2a0f2a0f2a0f2a0f2a0f0e0e0e0f29100e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f290f0f0e0e0e2a0f2a0f2a0f0e0e0e0f0e0e0e0f291029100e0e2a0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e2a0f2a0f2a0f291029100e0e2a0f0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e2a0f29100e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e2a0f2a0f0e0e2a0f0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f290f2a0f0e0f29100e0e0e0f0e0e0e0e0e0f0e0e0e0f29100e0e0e0f0e0e0e0e0e0f0e0e2a0f0e0f0e0e0e0f0e0e0e0e0e0f0e0e000d050000000000000000"
		}
	}
}
```

* See [Sample JSONs](#sample-jsons) for more room and device examples

### Users
* Server will validate both Telegram user's ID and Name
* User will not be able to delete him/her self
* Key value of Telegram User ID -> User Name
* We have to add at least one user and add other users through the first user

eg. Add John's Telegram account with user ID 1234567 and username `John`
```
{
	"1234567": "John"
}
```


### Sample JSONs
* [Rooms & Devices](docs/sample_devices.json)
* [Commands](docs/sample_commands.json)
* [Users](docs/sample_users.json)


### Learning Commands
#### Pre-requisites
* Your various broadlink devices are configured and running in your network (Refer to [Blackbean Device](#blackbean-devices), for more details)
* Completed Step 1-6 in [Run](#run)
* Installed `python-broadlink` library as we will be using the `cli` commands [here](https://github.com/mjg59/python-broadlink/tree/master/cli)
* Make sure you have your device(s) Type / IP / MAC Address
* To retrieve device(s) IP / MAC address
  1. Open `python` shell
  1. Import python-broadlink library `import broadlink`
  1. Discover all devices `devices = broadlink.discover(timeout=5)`
  1. To get a list of discovered devices in the variable devices eg. `[<broadlink.rm at 0x45a62f0>]`
  1. We need the device's type, host (IP), and MAC address information
  1. Iterate all discovered devices print details
  1. For learning use `for d in devices: print(d.type, d.host, "".join(format(x, '02x') for x in d.mac))`
  1. To get human format use `for d in devices: print(d.type, d.host, "".join(format(x, '02x') for x in reversed(d.mac))))`
  1. Save this information which will be used with the `cli` app
* Note: This step is currently done manually on shell, the ideal future will be a conversation
command in the app to **add / remove / edit** device commands

#### Steps to learn IR command
1. Make sure you have your device(s) Type / IP / MAC Address (refer to Pre-requisites for more details)
1. Refer to library's `type` definitions [here](https://github.com/mjg59/python-broadlink/blob/master/broadlink/__init__.py#L31)
1. `./vendor/python-broadlink/cli/broadlink_cli --type <type> --host <host> --mac <mac address> --learn`
1. `cli` app will output `Learning...`. Your blackbean device should light indicator will light up as **white**
![In Learning Mode](docs/screenshots/rm3_learning.jpg =240x320)
1. When it's white, point your IR remote controller to the top of the device and press the selected feature to learn the command
1. The `cli` app will print out the learned data, save this value in `commands.json` (See [Device's Commands](#device-commands) for more details on how to save learned command)
1. To test learned data with `./vendor/python-broadlink/cli/broadlink_cli --type <type> --host <host> --mac <mac address> --send <learned data>`


### Usage
#### Controlling devices
* Type `/keyboard` to begin
* First menu is to select the room ![Select Room](docs/screenshots/keyboard_start.jpg)
* After selecting the room, select the device in the room ![Select Device](docs/screenshots/keyboard_room_device.jpg)
* The device's feature buttons will be shown ![Device feature buttons](docs/screenshots/keyboard_device_features.jpg)
* On successful sent of the selected feature, the app will receive a feedback from the server
* eg. App
![App successful](docs/screenshots/keyboard_feature_query_alert_app.jpg =424x278)
* eg. Mobile
![Mobile successful](docs/screenshots/keyboard_feature_query_alert_mobile.jpg =292x501)
* Use `Back` or `Jump to Rooms` button to go back to previous menu or jump to select room menu
* To close, press the `Close` button and the menu keyboard will be closed
![Closed](docs/screenshots/keyboard_close.jpg)

#### Add Users
* Conversation Flow (Start conversation -> input user's user_id -> input user's username -> Successfully added user)
* Type `/adduser` to begin
* Input the user's user id (The user may use `/ping` to find out user id)
* Input the user's user name
* Success!
* At any time if you wish to cancel adding user, you can type `/canceladduser` to cancel add user conversation
* Type `/user` or `/status` to check which user(s) are approved
![Add User Example conversation flow](docs/screenshots/add_user.jpg)

#### Remove Users
* Type `/user` to begin
* Select `Delete User` next to the user you want to remove
![Remove User](docs/screenshots/remove_user.jpg)
* Select `Yes` to confirm removal of the selected user or `No` to go back to user menu keyboard ![Confirmation of removal user](docs/screenshots/remove_user_yes_no.jpg)
* On successful removal of selected user, the app will receive a feedback from the server stating the user's id and name that was removed
![Remove success query alert](docs/screenshots/remove_successful_query_alert.jpg)
* To close, press the `Close` button and the user menu keyboard will be closed


# Run
1. Make sure you have Python 3.3++
1. Clone or fork the repository (https://github.com/joh90/iot)
1. Install virtualenv and create virtualenv for the repository (`python -m virtualenv .virtualenv`)
1. Go to folder `cd iot`
1. Init submodules `git submodule init`
1. Install requirements `pip install -r requirements.txt`
1. Update `devices.json` with configuration
1. Add your telegram user to `users.json` (Refer to [Users](#users))
1. Run! ```python main.py --bot_id <BOT_ID> --bot_secret <BOT_SECRET> --name <Bot Name>```
1. You can pass your own users and devices json file, `python main.py --help` for more details
1. Find and add your bot to your Telegram
1. Enjoy!


# Screenshots
### Commands
#### Start (/start)
* Desktop App
![Start Command](docs/screenshots/command_start.jpg)
* Mobile App
![Start Command, mobile app](docs/screenshots/command_start_mobile.jpg =295x501)

#### Ping (/ping)
* Desktop App
![Ping Command](docs/screenshots/command_ping.jpg)

* Mobile App
![Ping Command, mobile](docs/screenshots/command_ping_mobile.jpg =295x501)

#### Status (/status)
* Desktop App
![Status Command](docs/screenshots/command_status.jpg)

* Mobile App
![Status Command, mobile](docs/screenshots/command_status_mobile.jpg =295x501)

#### List (/list)
* Desktop App
![List Command](docs/screenshots/command_list.jpg)

#### Keyboard (/keyboard)
* Desktop App
![Keyboard Command](docs/screenshots/keyboard_start.jpg)
![Keyboard Room's device](docs/screenshots/keyboard_room_device.jpg)
![Device's features](docs/screenshots/keyboard_device_features.jpg)

* Mobile App
![Keyboard Command, mobile](docs/screenshots/keyboard_start_mobile.jpg =295x501)
![Keyboard Room's device, mobile](docs/screenshots/keyboard_room_device_mobile.jpg =295x501)
![Device's features, mobile](docs/screenshots/keyboard_device_features_mobile.jpg =295x501)

#### User (/user)
* Desktop App
![User Command](docs/screenshots/command_user.jpg)

* Mobile App
![User Command, mobile](docs/screenshots/command_user_mobile.jpg =295x501)


# Videos
### Turning on/off Aircon
**To be added**

### Turning on/off TV
**To be added**


# Future
### Minor Improvements
- [ ] Improve Room / Device listing
- [ ] Use 2 columns if Inline Keyboard menu buttons are more than 8
- [ ] On / Off device in room keyboard menu level
- [ ] Sort device's feature with power on and off first
- [ ] Send / Backup JSONs

### Major Features
- [ ] Room / User last command (with time)
- [ ] Custom Macros
- [ ] Favorite Keyboard, custom make your own keyboard buttons
- [ ] Room / Device CRUD (conversation style)
- [ ] Learn device command (conversation style) (+ if device command cannot be found / convo command)
- [ ] Set Top Box network provider channel bindings
- [ ] Support TC2 light switches
- [ ] Support Multimedia device's menu into up / down / left / right keyboard
- [ ] Support Broadlink power strip
- [ ] Restart server
- [ ] Triggers with scheduler (on specific day / time of day / repeatable to do something)


# Limitation
* You have to learn your own device's command if it is not available from the project
* With IR devices, the server will not be able to know the current state, only last send commands
* If the home / office's internet goes down, you will not be able to send command to your devices
* If your computer / Raspberry-PI hosting the server dies / restart, you will not be able to send command to your devices
* 2 devices in the same room may result in both devices receiving the IR commands sent from the server


# Contributing
* Fork the project
* Create branch in your fork project (from master)
* Make and test your changes
* Commit to the branch you created (with meaningful commit messages)
* From your fork project, create a pull request (with meaningful comments with purpose of the change)
* Fix comments from reviewers (if any)
* Admins will merge your pull request, when it has enough approval
* Thanks for contributing!

# License
* MIT License

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.
