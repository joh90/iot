from functools import wraps

from iot.constants import (
    DEVICE_NOT_FOUND, USER_NOT_ALLOWED
)


def valid_device(func):
    @wraps(func)
    def wrapper(server, bot, update, *args, **kwargs):
        device_id = "".join(kwargs.get("args"))

        if device_id not in server.devices:
            update.message.reply_markdown(DEVICE_NOT_FOUND.format(device_id))
            return

        device = server.devices[device_id]

        return func(server, bot, update, device, *args, **kwargs)

    return wrapper


def valid_user(func):
    @wraps(func)
    def wrapper(server, bot, update, *args, **kwargs):
        # We save the user_id as string in the json file
        user_id = str(update.effective_user.id)
        user_name = update.effective_user.username

        if (
            user_id in server.approved_users and
            server.approved_users[user_id] == user_name
        ):
            return func(server, bot, update, *args, **kwargs)

        update.message.reply_text(USER_NOT_ALLOWED)

        return

    return wrapper
