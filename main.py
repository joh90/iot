import logging

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

logger = logging.getLogger(__name__)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(prog='main.py')

    parser.add_argument('--bot_id',
        required=True,
        help='Telegram Bot ID',
    )
    parser.add_argument('--bot_secret',
        required=True,
        help='Telegram Bot Secret'
    )
    parser.add_argument('--name',
        default='JoH IOT',
        help='Telegram IOT Server name, you can give it any name'
    )

    parser.add_argument('--devices',
        default='devices.json',
        help='Rooms & Devices JSON file path, defaults to `devices.json`'
    )
    parser.add_argument('--commands',
        default='commands.json',
        help='Commands file path, defaults to `commands.json`'
    )
    parser.add_argument('--users',
        default='users.json',
        help='Users file path, defaults to `users.json`'
    )

    args = parser.parse_args()

    from iot.server import iot_server
    iot_server.start_server(
        args.bot_id, args.bot_secret, args.name,
        args.devices, args.commands, args.users
    )
