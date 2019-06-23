from datetime import datetime
import logging

from telegram import (
    InlineKeyboardButton
)

from iot.rooms import d_factory
from iot.utils.keyboard.base import (
    CLOSE_INLINE_KEYBOARD_COMMAND,
    InlineKeyboardMixin,
    KeyboardCallBackQueryHandler
)


logger = logging.getLogger(__name__)


JUMP_ROOMS_TEXT = "Jump to Rooms"
BACK_TEXT = "<- Back"
CLOSE_TEXT = "Closed! /keyboard to reactivate keyboard"


class CommandKeyboardCBHandler(KeyboardCallBackQueryHandler, InlineKeyboardMixin):

    def func_name_to_text(self, name):
        return name.replace("_", " ")

    def jump_rooms_button(self):
        return InlineKeyboardButton(
            JUMP_ROOMS_TEXT, callback_data=self.return_cb_data("rooms")
        )

    def footer_buttons(self, target, target_type):
        button_list = [
            self.back_button(target, target_type),
            self.close_button()
        ]

        # Add Jump rooms button if target_type is device
        if target_type == "device":
            button_list.insert(0, [self.jump_rooms_button()])

        return button_list

    def back_button(self, back_target, target_type):
        cb_data = None

        # Rooms top level keyboard
        if target_type == "rooms":
            text = "Top Menu"
            cb_data = "rooms"
        # Room second level keyboard (listing devices), Back to Rooms kb
        elif target_type == "room":
            text = BACK_TEXT
            cb_data = back_target
        # Devices first level (listing device features), Back to Room kb
        elif target_type == "device":
            text = BACK_TEXT
            cb_data = back_target

        return InlineKeyboardButton(
            text, callback_data=self.return_cb_data(cb_data)
        )

    def construct_keyboard_markup(
        self, options, back_target, target_type, cols=0
    ):
        button_list = [
            InlineKeyboardButton(
                name, callback_data=self.return_cb_data(command)) \
                for name, command in options.items()
        ]

        footer_buttons = self.footer_buttons(back_target, target_type)

        keyboard = self.build_keyboard(button_list, cols=cols,
            footer_buttons=footer_buttons)

        markup = self.build_inline_keyboard_markup(keyboard)

        return markup

    def build_rooms_keyboard(self):
        rooms_data = dict((r, r) for r in self.server.rooms.keys())

        markup = self.construct_keyboard_markup(rooms_data, None, "rooms")

        return markup

    def build_room_devices_keyboard(self, room):
        room = self.server.rooms[room]

        rooms_devices_data = dict((d, d) for d in room.DEVICES.keys())
        rooms_broadlink_devices_data = dict(
            (d, d) for d in room.BL_DEVICES.keys()
        )

        rooms_devices_data.update(rooms_broadlink_devices_data)

        markup = self.construct_keyboard_markup(
            rooms_devices_data, "rooms", "room")

        return markup

    def build_device_keyboard(self, device):
        device = self.server.devices[device]

        device_interface = d_factory.get_device_type_interface(device.device_type)

        command = "{} {}"
        interface_data = dict(
            (self.func_name_to_text(i), command.format(device.id, i)) \
            for i in device_interface
        )

        markup = self.construct_keyboard_markup(
            interface_data, device.room.name, "device"
        )

        return markup

    def process_query(self, bot, update, internal_callback_data):
        query, query_data = super(CommandKeyboardCBHandler, self).process_query(
            bot, update, internal_callback_data)
        query_data_length = len(query_data)

        # Single length callback_data eg. room, tv
        if query_data_length == 1:
            query_data = query_data[0]

            if query_data in self.server.rooms.keys():
                self.handle_room(query_data, query, bot, update)
            elif query_data in self.server.devices.keys():
                self.handle_device(query_data, query, bot, update)
            elif query_data == "rooms":
                self.top_menu(query, bot, update)
            elif query_data == CLOSE_INLINE_KEYBOARD_COMMAND:
                self.handle_close(CLOSE_TEXT, query, bot, update)
        # Actual device feature command callback_data eg. aircon powerful
        elif query_data_length == 2:
            device_id = query_data[0]
            feature = query_data[1]

            device = self.server.devices[device_id]

            # Call server call_device
            self.server.call_device(
                bot, update, device, feature,
                handler_name=self.handler_name
            )
            # Update server last command handled
            self.server.last_command_handled = (
                self.__class__.__name__, device_id, feature,
                str(datetime.now()).split(".")[0]
            )

    def handle_room(self, room_name, query, bot, update):
        reply_markup = self.build_room_devices_keyboard(room_name)

        bot.edit_message_text(text="Select {} device".format(room_name),
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=reply_markup)

        self.answer_query(query, bot)

    def handle_device(self, device_id, query, bot, update):
        reply_markup = self.build_device_keyboard(device_id)

        bot.edit_message_text(text="Select {} feature".format(device_id),
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=reply_markup)

        self.answer_query(query, bot)

    def top_menu(self, query, bot, update):
        # To prevent "Message is not modified" from raising
        # as we should not be editing the message if it's in top menu
        if query.message.text == "Select room":
            self.answer_query(query, bot, text="Already at top menu!")
            return

        reply_markup = self.build_rooms_keyboard()

        bot.edit_message_text(text="Select room",
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=reply_markup)

        self.answer_query(query, bot)
