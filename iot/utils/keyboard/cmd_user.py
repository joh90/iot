from datetime import datetime
import logging

from telegram import (
    InlineKeyboardButton,
    ParseMode
)

from iot.rooms import d_factory
from iot.utils.keyboard.base import (
    CLOSE_INLINE_KEYBOARD_COMMAND,
    InlineKeyboardMixin,
    KeyboardCallBackQueryHandler
)


logger = logging.getLogger(__name__)


USERS_MENU = "users"
TOP_MENU_TEXT = "Approved Users"

BACK_TEXT = "<- Back"
CLOSE_TEXT = "Closed! /user to reactivate keyboard"
DELETE_USER_TEXT = "Delete user"
SELF_DELETE_RESPONSE_TEXT = "Unable to delete self"
DELETE_PROMPT_TEXT = "Are you sure you want to delete *{}* with {} id?"
DELETE_USER_RESPONSE_TEXT = "Deleted {} with {} id"

ADD_USER_INLINE_KEYBOARD_COMMAND = "add_user"
ADD_USER_TEXT = "Add User"
ADD_USER_RESPONSE_TEXT = "Use /adduser to add user"

USER_FUTURE = "This is a future feature, does nothing now"



PROMPT_DELETE = "promptdelete"
DELETE = "delete"


class CommandUserCBHandler(KeyboardCallBackQueryHandler, InlineKeyboardMixin):

    """
    List and delete approved users
    For adding users, see ConversationHandler for /adduser
    """
    def construct_keyboard_markup(
        self, options, target_type, cols=0
    ):
        prompt_delete_command = "{} {}"
        button_list = [
            [
                InlineKeyboardButton(
                    name, callback_data=self.return_cb_data(command)),
                InlineKeyboardButton(
                    DELETE_USER_TEXT, callback_data=self.return_cb_data(
                        prompt_delete_command.format(PROMPT_DELETE, command)
                ))
            ] \
            for name, command in options.items()
        ]

        header_buttons = self.header_buttons()
        footer_buttons = self.footer_buttons(target_type)

        keyboard = self.build_keyboard(button_list, cols=cols,
            header_buttons=header_buttons, footer_buttons=footer_buttons)

        markup = self.build_inline_keyboard_markup(keyboard)

        return markup

    def header_buttons(self):
        button_list = [
            InlineKeyboardButton(
                ADD_USER_TEXT,
                callback_data=self.return_cb_data(ADD_USER_INLINE_KEYBOARD_COMMAND))
        ]

        return button_list

    def footer_buttons(self, target_type):
        button_list = [
            self.back_button(target_type),
            self.close_button()
        ]

        return button_list

    def back_button(self, target_type):
        if target_type == USERS_MENU:
            text = "Top Menu"
        else:
            text = BACK_TEXT

        return InlineKeyboardButton(
            text, callback_data=self.return_cb_data(USERS_MENU)
        )

    def build_users_keyboard(self):
        """
        Build list of approved users with the bot server
        With delete button (X) next to user_name
        """
        users = self.server.approved_users

        users_data = dict(
            (user_name, user_id) \
            for user_id, user_name in users.items()
        )

        markup = self.construct_keyboard_markup(users_data, USERS_MENU)

        return markup

    def process_query(self, update, context, internal_callback_data):
        query, query_data = super(CommandUserCBHandler, self).process_query(
            update, context, internal_callback_data)
        query_data_length = len(query_data)

        # # Single length callback_data
        if query_data_length == 1:
            query_data = query_data[0]

            if query_data in self.server.approved_users.keys():
                self.handle_user(query, update, context)
            elif query_data == ADD_USER_INLINE_KEYBOARD_COMMAND:
                self.handle_add_user(query, update, context)
            elif query_data == USERS_MENU:
                self.top_menu(query, update, context)
            elif query_data == CLOSE_INLINE_KEYBOARD_COMMAND:
                self.handle_close(CLOSE_TEXT, query, update, context)
        # Actual user delete prompt and delete
        elif query_data_length == 2:
            action = query_data[0]
            user_id = query_data[1]

            if action == PROMPT_DELETE:
                deleter = str(update.effective_user.id)
                if deleter == user_id:
                    self.answer_query(query, context, text=SELF_DELETE_RESPONSE_TEXT)
                    return

                self.handle_delete_prompt(user_id, query, update, context)
            elif action == DELETE:
                self.handle_delete_user(user_id, query, update, context)

    def handle_user(self, query, update, context):
        # TODO: Future feature to list details for user
        self.answer_query(query, context, text=USER_FUTURE)

    def handle_add_user(self, query, update, context):
        context.bot.send_message(
            chat_id=query.message.chat_id,
            text=ADD_USER_RESPONSE_TEXT
        )

        self.answer_query(query, context)

    def top_menu(self, query, update, context):
        # To prevent "Message is not modified" from raising
        # as we should not be editing the message if it's in top menu
        if query.message.text == TOP_MENU_TEXT:
            self.answer_query(query, context, text="Already at top menu!")
            return

        reply_markup = self.build_users_keyboard()

        context.bot.edit_message_text(text=TOP_MENU_TEXT,
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=reply_markup)

        self.answer_query(query, context)

    def handle_delete_prompt(self, user_id, query, update, context):
        # YES answer to delete the user
        yes_cb = "{} {}".format(DELETE, user_id)
        # NO answer will result back to top menu
        no_cb = USERS_MENU

        reply_markup = self.construct_yes_no_prompt_keyboard(yes_cb, no_cb)

        user_name = self.server.approved_users[user_id]

        context.bot.edit_message_text(text=DELETE_PROMPT_TEXT.format(user_name, user_id),
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN)

        self.answer_query(query, context)

    def handle_delete_user(self, user_id, query, update, context):
        user_name = self.server.approved_users[user_id]

        del self.server.approved_users[user_id]
        logger.info("Deleted user from server %s %s", user_name, user_id)

        self.answer_query(query, context,
            text=DELETE_USER_RESPONSE_TEXT.format(user_name, user_id)
        )

        # Return to top menu
        self.top_menu(query, update, context)
