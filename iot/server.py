from datetime import datetime, timedelta
import json
import logging

from typing import Dict, List

import broadlink

from telegram import ReplyKeyboardMarkup
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler
)

from iot import constants
from iot.conversations.cmd_adduser import AddUserConversation
from iot.devices.base import BaseDevice
from iot.devices.errors import (
    CommandNotFound, InvalidArgument
)
from iot.rooms import Room
from iot.utils import return_mac
from iot.utils.decorators import (
    valid_device, valid_device_or_room,
    valid_device_feature, valid_user
)
from iot.utils.keyboard.cmd_keyboard import (
    CommandKeyboardCBHandler
)
from iot.utils.keyboard.cmd_user import (
    TOP_MENU_TEXT as USER_TOP_MENU_TEXT,
    CommandUserCBHandler
)


logger = logging.getLogger(__name__)


KEYBOARD_HANDLER_NAME = "/keyboard"
USER_HANDLER_NAME = "/user"


class TelegramIOTServer:

    def __init__(self):
        # Telegram Bot settings
        self.bot_id = None
        self.bot_secret = None
        self.bot_name = None

        # Telegram
        self.updater: Updater = None
        self.dp = None

        # JSON files path
        self.devices_path = None
        self.commands_path = None
        self.users_path = None

        # Broadlink and Devices
        self.blackbean_devices = {}
        self.rooms = {}
        self.devices = {}
        self.commands = {}
        self.approved_users = {}

        # Keyboard query handlers
        self.kb_handlers = {}

        # Others
        self.start_time: datetime = None
        self.last_command_handled = None

    def start_server(self, bot_id, bot_secret, bot_name,
            devices_path, commands_path, users_path):
        self.bot_id = bot_id
        self.bot_secret = bot_secret
        self.bot_name = bot_name

        self.devices_path = devices_path
        self.commands_path = commands_path
        self.users_path = users_path

        self.discover_blackbean_device()

        self.reload_commands()
        self.reload_rooms_and_devices()
        self.reload_users()

        self.kb_handlers[KEYBOARD_HANDLER_NAME] = \
            CommandKeyboardCBHandler(self, KEYBOARD_HANDLER_NAME)

        self.kb_handlers[USER_HANDLER_NAME] = \
            CommandUserCBHandler(self, USER_HANDLER_NAME)

        self.init_telegram_server()

    def init_telegram_server(self):
        token: str = '{}:{}'.format(self.bot_id, self.bot_secret)
        self.updater = Updater(
            token,
            user_sig_handler=self.stop_server
        )

        # Get the dispatcher to register handlers
        self.dp = self.updater.dispatcher

        self.dp.add_handler(CommandHandler("start", self.command_start))
        self.dp.add_handler(CommandHandler("ping", self.command_ping))
        self.dp.add_handler(CommandHandler("status", self.command_status))
        self.dp.add_handler(CommandHandler("list", self.command_list))
        self.dp.add_handler(CommandHandler(
            "keyboard", self.command_keyboard, pass_args=True
        ))
        self.dp.add_handler(CommandHandler(
            "user", self.command_user, pass_args=True
        ))
        self.dp.add_handler(CallbackQueryHandler(self.handle_keyboard_response))
        self.dp.add_handler(CommandHandler(
            "on", self.command_on, pass_args=True
        ))
        self.dp.add_handler(CommandHandler(
            "off", self.command_off, pass_args=True
        ))
        self.dp.add_handler(CommandHandler(
            "d", self.command_device, pass_args=True
        ))

        self.add_conversations()

        self.dp.add_error_handler(self.error)

        self.updater.start_polling()
        logger.info("Telegram IOT Server Running...")
        self.start_time = datetime.now()

        self.updater.idle()

    def add_conversations(self):
        # TODO: make a map and initialize it?
        AddUserConversation(
            self, ["adduser"], ["canceladduser"]
        )

    def reload_rooms_and_devices(self):
        try:
            with open(self.devices_path) as f:
                try:
                    data: dict = json.load(f)

                    if len(data) == 0:
                        logger.warning(
                            "Please add your rooms and devices to %s",
                            self.devices_path
                        )
                        return
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
        except FileNotFoundError as e:
            logger.error("Devices file not found %s", self.devices_path)
            raise e

    def find_blackbean_device(self, mac, bb_type):
            bb = self.blackbean_devices.get(mac)

            if bb and bb_type == bb.type:
                return bb

    def discover_blackbean_device(self):
        logger.info("Discovering Blackbean devices...")
        bb_devices: list = broadlink.discover(timeout=5)

        for bb in bb_devices:
            bb.auth()
            mac = return_mac(bb.mac)
            self.blackbean_devices[mac] = bb
            logger.info("Discovered %s device with %s mac", bb.type, mac)

    def reload_commands(self):
        try:
            with open(self.commands_path) as f:
                try:
                    data: dict = json.load(f)
                except (ValueError, TypeError) as e:
                    logger.error("Decoding commands json file failed: %s", e)
                    return
                else:
                    self.commands = data
        except FileNotFoundError as e:
            logger.error("Commands file not found %s", self.commands_path)
            raise e

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
        try:
            with open(self.users_path) as f:
                try:
                    data: Dict[str, str] = json.load(f)

                    if len(data) == 0:
                        logger.warning(
                            "Please populate at least one user to %s",
                            self.users_path
                        )
                except (ValueError, TypeError) as e:
                    logger.error("Decoding users json file failed: %s", e)
                    return
                else:
                    self.approved_users = data
        except FileNotFoundError as e:
            logger.error("Users file not found %s", self.users_path)
            raise e

    def save_users(self):
        logger.info("Saving approved users to %s", self.users_path)

        with open(self.users_path, "w") as f:
            try:
                json.dump(self.approved_users, f, indent=4, sort_keys=True)
            except Exception as e:
                logger.error("Error while saving users to json file: %s", e)

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
        update.message.reply_markdown(
            constants.START_MESSAGE.format(self.bot_name)
        )

    def command_ping(self, bot, update):
        """Sends message `pong` and user's id, name"""
        user = update.effective_user
        update.message.reply_markdown(constants.PONG_MESSAGE.format(
            user.username, user.id
        ))

    @valid_user
    def command_status(self, bot, update):
        """Sends server status back"""
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
            "\n".join(rooms_info)
        ))

    @valid_user
    @valid_device_or_room(compulsory=False)
    def command_keyboard(self, bot, update, *args, **kwargs):
        handler = self.kb_handlers[KEYBOARD_HANDLER_NAME]

        # By default, reply markup will be rooms keyboard
        reply_markup = handler.build_rooms_keyboard()
        text = "Select room"

        room = kwargs.pop("room", None)
        device = kwargs.pop("device", None)

        # If room, device can be found, that will be the markup
        if room:
            reply_markup = handler.build_room_devices_keyboard(room.name)
            text = "Select {} device".format(room.name)
        elif device:
            reply_markup = handler.build_device_keyboard(device.id)
            text = "Select {} feature".format(device.id)

        update.message.reply_text(text, reply_markup=reply_markup)

    @valid_user
    def command_user(self, bot, update, *args, **kwargs):
        handler = self.kb_handlers[USER_HANDLER_NAME]

        reply_markup = handler.build_users_keyboard()

        update.message.reply_text(USER_TOP_MENU_TEXT,
            reply_markup=reply_markup)

    def handle_keyboard_response(self, bot, update):
        query = update.callback_query
        handler_name, internal_cb_data = query.data.split(" ", 1)

        if handler_name in self.kb_handlers.keys():
            self.kb_handlers[handler_name].process_query(
                bot, update, internal_cb_data
            )
        else:
            logger.error(
                "Unable to find handler_name, %s in kb_handlers",
                handler_name
            )

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
        action can be found, passthrough function and call
        server's call_device method
        """
        self.call_device(bot, update, device, feature,
            action=action, *args, **kwargs)

    def call_device(self, bot, update, device, feature,
            action=None, handler_name=None, *args, **kwargs):
        """
        Call specified device's feature and action,
        if it can be found
        """
        def send_text(bot, update, message):
            if update.callback_query:
                self.kb_handlers[handler_name].answer_query(
                    update.callback_query, bot,
                    text=message
                )
            else:
                update.message.reply_markdown(message)

        func = getattr(device, feature)

        new_args = args

        if action:
            new_args = (action,)

        try:
            func(*new_args)

            if update.callback_query:
                self.kb_handlers[handler_name].answer_query(
                    update.callback_query, bot,
                    text="Sent {} with {}".format(device.id, feature)
                )
        except (NotImplementedError, CommandNotFound):
            action = '' if not action else action

            send_text(bot, update,
                constants.DEVICE_COMMAND_NOT_IMPLEMENTED.format(
                    device.id, feature, action)
            )
        except (TypeError, InvalidArgument):
            send_text(bot, update, constants.ARGS_ERROR)

    def stop_server(self, *args, **kwargs):
        logger.info("Telegram IOT Server stopping...")

        # Save approved users json
        # TODO: optimize this, save only on approved_users change
        self.save_users()


server: TelegramIOTServer = TelegramIOTServer()
