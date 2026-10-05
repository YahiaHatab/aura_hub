"""Main entry point for Aura Hub — Navidrome Management & Audio Tagging Suite.

Initializes the python-telegram-bot ApplicationBuilder, configures bot commands,
and mounts modular routers from the handlers package.
"""

import logging
import threading
import uvicorn
from telegram import BotCommand, MenuButtonWebApp, WebAppInfo
from telegram.ext import Application, ApplicationBuilder

import config
from handlers import common, download, library, navidrome, nowplaying, request, settings, system, users

# Configure application logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("aura_hub")


async def post_init(application: Application) -> None:
    """Configures bot menu commands and WebApp chat menu button."""
    commands = [
        BotCommand("hub", "Open Aura Hub WebApp dashboard"),
        BotCommand("nowplaying", "Live playback sessions on Navidrome"),
        BotCommand("request", "Request music for ingestion"),
        BotCommand("search", "Search tracks on YouTube with interactive buttons"),
        BotCommand("quality", "Set audio download quality preference"),
        BotCommand("download", "Download Spotify/YouTube URL (Admin)"),
        BotCommand("genius", "Download with explicit Genius album match (Admin)"),
        BotCommand("retag", "Browse folders to update tags & .lrc (Admin)"),
        BotCommand("delete", "Permanently remove an album folder (Admin)"),
        BotCommand("rescan", "Trigger instant Navidrome library scan (Admin)"),
        BotCommand("scanstatus", "View Navidrome scan status"),
        BotCommand("users", "Manage Navidrome user accounts (Admin)"),
        BotCommand("requests", "View music request queue (Admin)"),
        BotCommand("storage", "Check disk space & library stats"),
        BotCommand("status", "Check bot, tool, and Navidrome health"),
        BotCommand("help", "Show help and syntax guide"),
    ]
    await application.bot.set_my_commands(commands)

    # Set chat menu button to launch WebApp if external URL is provided
    if config.WEBAPP_EXTERNAL_URL:
        try:
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="⚡ Aura Hub",
                    web_app=WebAppInfo(url=config.WEBAPP_EXTERNAL_URL),
                )
            )
            logger.info("Chat menu button set to WebApp: %s", config.WEBAPP_EXTERNAL_URL)
        except Exception as e:
            logger.warning("Could not set chat menu button: %s", e)

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
        nowplaying.router,
        request.router,
        settings.router,
        system.router,
        users.router,
    ]

    for router in routers:
        for handler in router:
            app.add_handler(handler)

    logger.info("All handler routers successfully registered.")
    return app


def run_web_server() -> None:
    """Runs the FastAPI WebApp server via uvicorn in a dedicated thread."""
    try:
        cfg = uvicorn.Config(
            "services.web:app",
            host=config.WEBAPP_HOST,
            port=config.WEBAPP_PORT,
            log_level="info",
        )
        server = uvicorn.Server(cfg)
        server.run()
    except Exception as e:
        logger.error("Web server stopped with error: %s", e)


def main() -> None:
    """Starts the Aura Hub bot polling service and background web server."""
    if not config.TELEGRAM_BOT_TOKEN or "YOUR_TOKEN" in config.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set or invalid in config.py.")
        return

    # Start WebApp dashboard server in a daemon thread
    if config.WEBAPP_PORT:
        web_thread = threading.Thread(
            target=run_web_server, daemon=True, name="AuraWebAppThread"
        )
        web_thread.start()
        logger.info(
            "Aura Hub WebApp dashboard running locally on http://%s:%s",
            config.WEBAPP_HOST,
            config.WEBAPP_PORT,
        )

    logger.info("Starting Aura Hub Telegram Bot...")
    app = build_application()
    app.run_polling()


if __name__ == "__main__":
    main()

