from functools import wraps
from datetime import datetime

from iot.constants import (
    ON_OFF,
    DEVICE_NOT_FOUND, ROOM_OR_DEVICE_NOT_FOUND,
    ARGS_ERROR,
    DEVICE_FEATURE_ACTION_NOT_FOUND,
    USER_NOT_ALLOWED
)


def valid_device(func):
    @wraps(func)
    def wrapper(server, update, context, *args, **kwargs):
        device_id = "".join(context.args)

        if device_id not in server.devices:
            update.message.reply_markdown(DEVICE_NOT_FOUND.format(device_id))
            return

        device = server.devices[device_id]

        return func(server, update, context, device, *args, **kwargs)

    return wrapper


def valid_device_or_room(compulsory=True):
    def _inner(func, *args, **kwargs):
        @wraps(func)
        def wrapper(server, update, context, *args, **kwargs):
            user_params = "".join(context.args)
            room = None
            device = None

            if user_params in server.rooms:
                room = server.rooms[user_params]
            elif user_params in server.devices:
                device = server.devices[user_params]
            else:
                if compulsory:
                    update.message.reply_markdown(
                        ROOM_OR_DEVICE_NOT_FOUND.format(user_params)
                    )

                    return

            return func(
                server, update, context,
                room=room, device=device, *args, **kwargs
            )

        return wrapper

    return _inner


def valid_device_feature(func):
    @wraps(func)
    def wrapper(server, update, context, *args, **kwargs):
        user_params = context.args

        # device_id and at least one other params is sent
        if len(user_params) > 1:
            device_id = user_params[0]

            if device_id not in server.devices:
                update.message.reply_markdown(DEVICE_NOT_FOUND.format(device_id))
                return

            device = server.devices[device_id]
            feature = user_params[1].lower()

            # If 3 parameters are sent, we can potentially
            # join feature and action to find the attribute
            if len(user_params) > 2:
                action = user_params[2].lower()
                potential_feature = "_".join([feature, action])

                if callable(getattr(device, potential_feature, None)):
                    return func(
                        server, update, context, device, potential_feature,
                        *args, **kwargs
                    )

            if feature in ON_OFF:
                # Eg `/d aircon on`
                if len(user_params) == 2:
                    feature = 'power_' + feature
                # Eg `/d aircon power on`
                # elif len(user_params) == 3:
                #     action = user_params[2].lower()
                #     feature = "_".join([feature, action])
                #     action = None
                else:
                    update.message.reply_text(ARGS_ERROR)
                    return

                return func(
                    server, update, context, device, feature,
                    *args, **kwargs
                )

            if not callable(getattr(device, feature, None)):
                update.message.reply_text(ARGS_ERROR)
                return

            try:
                action = user_params[2].lower()
            except IndexError:
                action = None

            return func(
                server, update, context, device, feature,
                action=action, *args, **kwargs
            )

        # Cases where only device_id or no params is sent
        else:
            update.message.reply_text(ARGS_ERROR)

    return wrapper


def valid_user(func):
    @wraps(func)
    def wrapper(server, update, context, *args, **kwargs):
        # We save the user_id as string in the json file
        user_id = str(update.effective_user.id)
        user_name = update.effective_user.username

        if (
            user_id in server.approved_users and
            server.approved_users[user_id] == user_name
        ):
            # Update server last command handled
            # other than status and handling keyboard response
            if not func.__name__ in ("command_status", "handle_keyboard_response"):
                server.last_command_handled = (
                    func.__name__, args, kwargs,
                    str(datetime.now()).split(".")[0]
                )

            return func(server, update, context, *args, **kwargs)

        # Handle normal commands
        if getattr(update, "message"):
            update.message.reply_text(USER_NOT_ALLOWED)
        # Handles Inline KB queries
        elif getattr(update, "callback_query"):
            query = update.callback_query
            context.bot.edit_message_text(text=USER_NOT_ALLOWED,
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                reply_markup=None)

        return

    return wrapper


def valid_user_conversation(func):
    @wraps(func)
    def wrapper(conversation, update, context, *args, **kwargs):
        server = conversation.server

        # We save the user_id as string in the json file
        user_id = str(update.effective_user.id)
        user_name = update.effective_user.username

        if (
            user_id in server.approved_users and
            server.approved_users[user_id] == user_name
        ):
            # Update server last command handled, other than status
            if not func.__name__ == "command_status":
                server.last_command_handled = (
                    func.__name__, args, kwargs,
                    str(datetime.now()).split(".")[0]
                )

            return func(conversation, update, context, *args, **kwargs)

        update.message.reply_text(USER_NOT_ALLOWED)

        return

    return wrapper
