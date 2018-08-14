from functools import wraps

from iot.constants import (
    ON_OFF,
    DEVICE_NOT_FOUND, ARGS_ERROR,
    DEVICE_FEATURE_ACTION_NOT_FOUND,
    USER_NOT_ALLOWED
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


def valid_device_feature(func):
    @wraps(func)
    def wrapper(server, bot, update, *args, **kwargs):
        # TODO: We can go ahead and check the device
        # and send what is possible feature commands to send next time
        user_params = kwargs.get("args")

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
                        server, bot, update, device, potential_feature,
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
                    server, bot, update, device, feature,
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
                server, bot, update, device, feature,
                action=action, *args, **kwargs
            )

        # Cases where only device_id or no params is sent
        else:
            update.message.reply_text(ARGS_ERROR)

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
            # Update server last command handled, other than status
            if not func.__name__ == "command_status":
                server.last_command_handled = (func.__name__, args, kwargs)

            return func(server, bot, update, *args, **kwargs)

        update.message.reply_text(USER_NOT_ALLOWED)

        return

    return wrapper
