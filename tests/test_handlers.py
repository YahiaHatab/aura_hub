"""Integration and smoke tests for Telegram bot handler routers and application builder."""

import unittest
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from handlers import common, download, library, navidrome, system
from main import build_application


class TestHandlersAndApplication(unittest.TestCase):
    def test_routers_contain_handlers(self):
        self.assertGreater(len(common.router), 0)
        self.assertGreater(len(download.router), 0)
        self.assertGreater(len(library.router), 0)
        self.assertGreater(len(navidrome.router), 0)
        self.assertGreater(len(system.router), 0)

    def test_build_application(self):
        app = build_application()
        self.assertIsNotNone(app)

        # Collect registered command names across all handler groups
        registered_commands = set()
        has_callback_handler = False
        has_message_handler = False

        for group_handlers in app.handlers.values():
            for handler in group_handlers:
                if isinstance(handler, CommandHandler):
                    registered_commands.update(handler.commands)
                elif isinstance(handler, CallbackQueryHandler):
                    has_callback_handler = True
                elif isinstance(handler, MessageHandler):
                    has_message_handler = True

        expected_commands = {
            "start",
            "help",
            "status",
            "download",
            "genius",
            "search",
            "retag",
            "delete",
            "remove",
            "rescan",
            "scanstatus",
            "storage",
            "disk",
        }

        for cmd in expected_commands:
            self.assertIn(cmd, registered_commands, f"Command /{cmd} not registered in application")

        self.assertTrue(has_callback_handler, "No CallbackQueryHandlers registered")
        self.assertTrue(has_message_handler, "No MessageHandlers registered (auto link catcher missing)")


if __name__ == "__main__":
    unittest.main()
