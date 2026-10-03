"""Main entry point for Aura Hub — Navidrome Management & Audio Tagging Suite.

Initializes the python-telegram-bot ApplicationBuilder, configures bot commands,
and mounts modular routers from the handlers package.
"""

import logging
from telegram import BotCommand
from telegram.ext import Application, ApplicationBuilder

import config
from handlers import common, download, library, navidrome, system

# Configure application logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("aura_hub")


async def post_init(application: Application) -> None:
    """Configures bot menu commands visible in Telegram clients."""
    commands = [
        BotCommand("download", "Download Spotify/YouTube URL"),
        BotCommand("search", "Search tracks on YouTube with interactive buttons"),
        BotCommand("genius", "Download with explicit Genius album match"),
        BotCommand("retag", "Browse folders to update tags & .lrc"),
        BotCommand("delete", "Permanently remove an album folder"),
        BotCommand("rescan", "Trigger instant Navidrome library scan"),
        BotCommand("scanstatus", "View Navidrome scan status"),
        BotCommand("storage", "Check disk space & library stats"),
        BotCommand("status", "Check bot, tool, and Navidrome health"),
        BotCommand("help", "Show help and syntax guide"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands successfully registered with Telegram API.")


def build_application() -> Application:
    """Constructs the Application instance and registers all handler routers."""
    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Modular handler routers
    routers = [
        common.router,
        download.router,
        library.router,
        navidrome.router,
        system.router,
    ]

    for router in routers:
        for handler in router:
            app.add_handler(handler)

    logger.info("All handler routers successfully registered.")
    return app


def main() -> None:
    """Starts the Aura Hub bot polling service."""
    if not config.TELEGRAM_BOT_TOKEN or "YOUR_TOKEN" in config.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set or invalid in config.py.")
        return

    logger.info("Starting Aura Hub Telegram Bot...")
    app = build_application()
    app.run_polling()


if __name__ == "__main__":
    main()
