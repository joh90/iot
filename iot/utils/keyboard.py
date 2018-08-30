import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from iot.rooms import d_factory


logger = logging.getLogger(__name__)


BACK_TEXT = "<- Back"
CLOSE_INLINE_KEYBOARD_COMMAND = "close_keyboard"


def build_markup(options) -> list:
    keyboard = [
        [InlineKeyboardButton(name, callback_data=command)] \
        for name, command in options.items()
    ]

    markup = InlineKeyboardMarkup(keyboard)

    return markup


class KeyboardCallbackQueryHandler:

    server = None

    def __init__(self, server):
        self.server = server

    def func_name_to_text(self, name):
        return name.replace("_", " ")

    def answer_query(self, query, bot, text=None, alert=False):
        bot.answer_callback_query(query.id, text=text, show_alert=alert)

    def build_keyboard(self, buttons, cols, header_buttons=None, footer_buttons=None):
        if cols > 0:
            kb = [buttons[i:i + cols] for i in range(0, len(buttons), cols)]
        else:
            kb = [[b] for b in buttons]

        if header_buttons:
            kb.insert(0, header_buttons)
        if footer_buttons:
            kb.append(footer_buttons)

        return kb

    def footer_buttons(self, target, target_type):
        button_list = [
            self.back_button(target, target_type),
            self.close_button()
        ]

        return button_list

    def back_button(self, back_target, target_type):
        cb_data = None

        # Rooms top level keyboard
        if target_type == "rooms":
            text = "Top Menu"
            cb_data = 'rooms'
        # Room second level keyboard (listing devices), Back to Rooms kb
        elif target_type == "room":
            text = BACK_TEXT
            cb_data = back_target
        # Devices first level (listing device features), Back to Room kb
        elif target_type == "device":
            text = BACK_TEXT
            cb_data = back_target

        return InlineKeyboardButton(
            text, callback_data=cb_data
        )

    def close_button(self):
        return InlineKeyboardButton(
            "Close", callback_data=CLOSE_INLINE_KEYBOARD_COMMAND
        )

    def construct_keyboard_markup(
        self, options, back_target, target_type, cols=0
    ):
        button_list = [
            InlineKeyboardButton(name, callback_data=command) \
            for name, command in options.items()
        ]

        footer_buttons = self.footer_buttons(back_target, target_type)

        buttons = self.build_keyboard(button_list, cols=cols,
            footer_buttons=footer_buttons)

        markup = InlineKeyboardMarkup(buttons)

        return markup

    def build_rooms_keyboard(self):
        rooms_data = dict((r, r) for r in self.server.rooms.keys())

        markup = self.construct_keyboard_markup(rooms_data, None, "rooms")

        return markup

    def build_room_devices_keyboard(self, room):
        room = self.server.rooms[room]

        rooms_devices_data = dict((d, d) for d in room.DEVICES.keys())

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

    def process_query(self, bot, update):
        query = update.callback_query
        print('query', query)
        logger.info("Keyboard CB Handler: Handling '%s'", query.data)

        # query_data is only the device's id
        query_data = query.data.split()
        query_data_length = len(query_data)

        # Single length callback_data eg. room, tv
        if query_data_length == 1:
            if query.data in self.server.rooms.keys():
                self.handle_room(query.data, query, bot, update)
            elif query.data in self.server.devices.keys():
                self.handle_device(query.data, query, bot, update)
            elif query.data == "rooms":
                self.top_menu(query, bot, update)
            elif query.data == CLOSE_INLINE_KEYBOARD_COMMAND:
                self.handle_close(query, bot, update)
        # Actual device feature command callback_data eg. aircon powerful
        elif query_data_length == 2:
            device_id = query_data[0]
            feature = query_data[1]

            device = self.server.devices[device_id]

            # call server call_device
            print('do something here', device_id, feature)
            self.server.call_device(
                bot, update, device, feature
            )
            # Update server last command handled
            self.server.last_command_handled = (
                self.__class__.__name__, device_id, feature
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

    def handle_close(self, query, bot, update):
        bot.edit_message_text(text="Closed! /keyboard to reactivate keyboard",
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=None)

        self.answer_query(query, bot)
