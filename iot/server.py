from datetime import datetime, timedelta
import json
import logging

from typing import Dict, List

import broadlink

from telegram import ReplyKeyboardMarkup
from telegram.ext import (Updater, CommandHandler, MessageHandler, Filters, RegexHandler,
                          ConversationHandler)

from iot import constants
from iot.devices import BaseDevice, populate_devices
from iot.devices.errors import (
    CommandNotFound, InvalidArgument
)
from iot.rooms import Room
from iot.utils import return_mac
from iot.utils.decorators import (
    valid_device, valid_device_feature,
    valid_user
)


logger = logging.getLogger(__name__)

# TODO: send these to constructor instead
BOT_ID: str = ""
BOT_SECRET: str = ""

ROOM_DEVICES_FILE_PATH: str = ""
COMMANDS_FILE_PATH: str = ""
USERS_FILE_PATH: str = ""


class TelegramIOTServer:

    def __init__(self):
        # Telegram
        self.updater: Updater = None
        self.dp = None

        # Broadlink and Devices
        self.blackbean_devices = {}
        self.rooms = {}
        self.devices = {}
        self.commands = {}
        self.approved_users = {}

        self.start_time: datetime = None
        self.last_command_handled = None

    def start_server(self):
        # TODO: It should discover and check against the mac address of the device
        # hard code mac addr for now
        bb_devices: list = broadlink.discover(timeout=5)
        bb_devices[0].auth()
        self.blackbean_devices["780f771a192e"] = bb_devices[0]

        self.reload_commands()
        self.reload_rooms_and_devices()
        self.reload_users()

        self.init_telegram_server()

    def init_telegram_server(self):
        token: str = '{}:{}'.format(BOT_ID, BOT_SECRET)
        self.updater = Updater(token)

        # Get the dispatcher to register handlers
        self.dp = self.updater.dispatcher

        self.dp.add_handler(CommandHandler("start", self.command_start))
        self.dp.add_handler(CommandHandler("ping", self.command_ping))
        self.dp.add_handler(CommandHandler("status", self.command_status))
        self.dp.add_handler(CommandHandler("list", self.command_list))
        self.dp.add_handler(CommandHandler(
            "on", self.command_on, pass_args=True
        ))
        self.dp.add_handler(CommandHandler(
            "off", self.command_off, pass_args=True
        ))
        self.dp.add_handler(CommandHandler(
            "d", self.command_device, pass_args=True
        ))
        #self.dp.add_handler(CommandHandler("debug", debug))

        self.dp.add_error_handler(self.error)

        self.updater.start_polling()
        logger.info("Telegram IOT Server Running")
        self.start_time = datetime.now()

        self.updater.idle()

    def reload_rooms_and_devices(self):
        with open(ROOM_DEVICES_FILE_PATH) as f:
            try:
                data: dict = json.load(f)
            except (ValueError, TypeError) as e:
                logger.error("Decoding devices json file failed: %s", e)
                return
            else:
                for room_name, value in data.items():
                    if room_name not in self.rooms:
                        try:
                            mac_address: str = value["mac_address"]
                            blackbean_type: str = value["blackbean_type"]
                            bb_device = self.find_blackbean_device(
                                mac_address, blackbean_type
                            )

                            if bb_device:
                                r: Room = Room(room_name, bb_device)
                                pop_device: List[BaseDevice] = r.populate_devices(
                                    value.get("devices", []))

                                self.rooms[room_name] = r

                                for pd in pop_device:
                                    commands: Dict = self.get_commands(
                                        pd.device_type.value,
                                        pd.brand,
                                        pd.model
                                    )
                                    if commands:
                                        pd.populate_device_commands(commands)

                                    self.devices[pd.id] = pd

                        except Exception as e:
                            logger.error("Error While reloading rooms and devices: %s", e)
                            continue

    def find_blackbean_device(self, mac, bb_type):
            bb = self.blackbean_devices.get(mac)

            if bb and bb_type == bb.type:
                return bb

    def discover_blackbean_device(self):
        pass

    def reload_commands(self):
        with open(COMMANDS_FILE_PATH) as f:
            try:
                data: dict = json.load(f)
            except (ValueError, TypeError) as e:
                logger.error("Decoding commands json file failed: %s", e)
                return
            else:
                self.commands = data

    def get_commands(self, device_type, brand, model) -> Dict[str, str]:
        # TODO: Search for model next time

        # Always convert device_type to String,
        # as populated command dict key is in String
        device_type = str(device_type)

        try:
            return self.commands[device_type][brand]
        except KeyError:
            logger.error("Command not found for %s-%s-%s",
                device_type, brand, model)
            return {}

    def reload_users(self):
        with open(USERS_FILE_PATH) as f:
            try:
                data: Dict[str, str] = json.load(f)
            except (ValueError, TypeError) as e:
                logger.error("Decoding users json file failed: %s", e)
                return
            else:
                self.approved_users = data

    def error(self, bot, update, error):
        """Log Errors caused by Updates."""
        logger.warning('Update "%s" caused error "%s"', update, error)

    @property
    def uptime(self):
        now = datetime.now()
        uptime = str(
            timedelta(
                seconds=(now-self.start_time).total_seconds()
            )
        )

        return uptime

    @property
    def blackbean_devices_info(self):
        all_bb_info = []

        # TODO: move mac conversion to utils
        for bb in self.blackbean_devices.values():
            bb_info = "Type: {}, IP: {}, Mac: {}".format(
                bb.type, bb.host[0],
                return_mac(bb.mac)
            )
            all_bb_info.append(bb_info)

        return '\n'.join(all_bb_info)

    @valid_user
    def command_start(self, bot, update):
        """Send a message when the command `/start` is issued."""
        update.message.reply_markdown(constants.START_MESSAGE)

    def command_ping(self, bot, update):
        """Sends message `pong` and user's id, name"""
        user = update.effective_user
        update.message.reply_markdown(constants.PONG_MESSAGE.format(
            user.username, user.id
        ))

    @valid_user
    def command_status(self, bot, update):
        """Sends server status back"""
        print(datetime.now() - self.start_time)
        print(self.blackbean_devices)
        print(self.rooms)
        print(self.devices)
        #print(self.commands)
        print(self.rooms["office"].room_info())
        # self.blackbean_devices["780f771a192e"].send_data(
        #     bytearray.fromhex(''.join("26006c000f0717120707080d0613070d0707080805140613070d071306080709041a060e06000b820f06070905150608050f0713060e060805230713060e061307070529070d07000b810f06070905150607070e0713060e0607070805150713060e061307060709051a070d07000d05000000000000000000000000"))
        # )
        server_info = constants.STATUS_MESSAGE.format(
            str(datetime.now()).split(".")[0],
            self.uptime,
            self.last_command_handled,
            self.blackbean_devices_info,
            len(self.rooms),
            len(self.devices),
            ", ".join(self.approved_users.values())
        )

        update.message.reply_text(server_info)

    @valid_user
    def command_list(self, bot, update):
        rooms_info = [str(r.room_list_info()) for r in self.rooms.values()]

        update.message.reply_markdown(constants.LIST_MESSAGE.format(
            "\n\n".join(rooms_info)
        ))

    @valid_user
    @valid_device
    def command_on(self, bot, update, device, *args, **kwargs):
        """Turn ON targeted device if device id can be found"""
        device.power_on()

    @valid_user
    @valid_device
    def command_off(self, bot, update, device, *args, **kwargs):
        """Turn OFF targeted device if device id can be found"""
        device.power_off()

    @valid_user
    @valid_device_feature
    def command_device(self, bot, update, device, feature,
            action=None, *args, **kwargs
        ):
        """
        Command device specific feature if device_id, feature and
        action can be found
        """
        print(device, feature, action)
        func = getattr(device, feature)

        print('args', args)
        new_args = args

        if action:
            new_args = (action,)

        print('na', new_args)
        print('na_type', type(new_args))

        try:
            func(*new_args)
        except (NotImplementedError, CommandNotFound):
            action = '' if not action else action

            update.message.reply_markdown(
                constants.DEVICE_COMMAND_NOT_IMPLEMENTED.format(
                    device.id, feature, action
                )
            )
        except (TypeError, InvalidArgument):
            update.message.reply_text(constants.ARGS_ERROR)


    def stop_server(self):
        pass


server: TelegramIOTServer = TelegramIOTServer()
