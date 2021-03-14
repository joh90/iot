from telegram.ext import (
    CommandHandler, ConversationHandler,
    Filters, MessageHandler
)


class BaseConversations:
    """
    Attributes:
    server - TelegramBotServer server object
    entry_cmd - List of commands for entry_points,
        will be binded to self.start eg. ["/start"]
    fallback_cmd - List of commands for fallbacks,
        will be binded to self.cancel eg. ["/cancelstart"]
    """
    __slots__ = ("server", "entry_cmd", "fallback_cmd",
        "states", "handler")

    def __init__(self, server, entry_cmd, fallback_cmd, **kwargs):
        self.server = server
        self.entry_cmd = entry_cmd
        self.fallback_cmd = fallback_cmd

        self.register_conversations(**kwargs)

    def set_states(self):
        """
        Implement setting self.states with conversations flow
        and various handlers / filters with the class's method
        eg. {
            0: [RegexHandler('^(Boy|Girl|Other)$', self.gender)],

            1: [MessageHandler(Filters.photo, self.photo),
                    CommandHandler('skip', self.skip_photo)],

            2: [MessageHandler(Filters.location, self.location),
                       CommandHandler('skip', self.skip_location)],

            3: [MessageHandler(Filters.text, self.bio)]
        }
        """
        raise NotImplementedError

    def register_conversations(self, **kwargs):
        self.set_states()
        self.handler = ConversationHandler(
            entry_points=self.return_entry_points(),
            states=self.states,
            fallbacks=self.return_fallbacks(),
            **kwargs
        )

        self.server.dp.add_handler(self.handler)

    def return_entry_points(self):
        return [
            CommandHandler(command, self.start)
            for command in self.entry_cmd
        ]

    def return_fallbacks(self):
        return [
            CommandHandler(command, self.cancel)
            for command in self.fallback_cmd
        ]

    def start(self, update, context, *args, **kwargs):
        pass

    def cancel(self, update, context, *args, **kwargs):
        pass

    def end(self, update, context, *args, **kwargs):
        check = self.handler.check_update(update)
        self.handler.update_state(ConversationHandler.END, check[0])
