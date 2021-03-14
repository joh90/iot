import logging

from telegram.ext import (
    MessageHandler, Filters
)

from iot.conversations import BaseConversations
from iot.utils.decorators import valid_user_conversation


logger = logging.getLogger(__name__)


START_TEXT = "Please enter the user's user_id, (User can `/ping` to find out the user_id) (/canceladduser to cancel adding)"
USER_NAME_TEXT = "Please enter the user name for {}, (User can `/ping` to find out the user_name)"
USER_ID_EXISTS = "User exists! Please re-enter the user_id! {} already exists"
USER_ID_INT_ERROR = "Invalid parameters! Please enter the user_id in integers"
ADDED_USER_TEXT = "Added *{}*, id: {}, /user or /status to check approved users"
CANCEL_TEXT = "Cancelled /adduser conversation"


class AddUserConversation(BaseConversations):

    USER_ID, USER_NAME = range(2)

    def set_states(self):
        self.states = {
            self.USER_ID: [MessageHandler(
                Filters.text & ~Filters.command, self.add_user_id,
                pass_user_data=True
            )],
            self.USER_NAME: [MessageHandler(
                Filters.text & ~Filters.command, self.add_user_name,
                pass_user_data=True
            )],
        }

    @valid_user_conversation
    def start(self, update, context):
        update.message.reply_text(START_TEXT)

        return self.USER_ID

    def add_user_id(self, update, context, *args, **kwargs):
        user_id = update.message.text

        # Test if user_id can be casted to integer
        try:
            int(user_id)
        except ValueError:
            update.message.reply_text(USER_ID_INT_ERROR)
            return self.USER_ID

        if user_id in self.server.approved_users.keys():
            update.message.reply_text(USER_ID_EXISTS.format(user_id))
            return self.USER_ID

        user_data = context.user_data
        user_data["add_user_id"] = user_id

        update.message.reply_text(USER_NAME_TEXT.format(user_id))

        return self.USER_NAME

    def add_user_name(self, update, context, *args, **kwargs):
        #user_data = kwargs.pop("user_data", None)
        if not context.user_data:
            logger.error("No user_data in kwargs, unable to process")
            self.end(update, context)

        user_name = update.message.text
        add_user_id = context.user_data["add_user_id"]

        self.server.approved_users[add_user_id] = user_name

        logger.info("Adding user to server %s %s", user_name, add_user_id)

        # Remove `add_user_id` from user_data
        context.user_data.clear()

        update.message.reply_markdown(
            ADDED_USER_TEXT.format(user_name, add_user_id)
        )

        self.end(update, context)

    @valid_user_conversation
    def cancel(self, update, context):
        update.message.reply_text(CANCEL_TEXT)
        self.end(update, context)
