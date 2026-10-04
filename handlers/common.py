"""Common handlers: authentication, /start, /help, and /status."""

import functools
import logging
from typing import Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import CommandHandler, ContextTypes

import config
from services.navidrome import navidrome_client
from services.system import get_system_diagnostic_summary

logger = logging.getLogger(__name__)


def is_authorized(user_id: int) -> bool:
    """Checks whether the user ID is permitted to use the bot and submit requests."""
    return not config.ALLOWED_USER_IDS or user_id in config.ALLOWED_USER_IDS


def is_admin(user_id: int) -> bool:
    """Checks whether the user ID has administrative privileges."""
    return not config.ADMIN_USER_IDS or user_id in config.ADMIN_USER_IDS


def auth_required(func: Callable):
    """Decorator ensuring that only authorized Telegram users can trigger a command."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user or not is_authorized(user.id):
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Unauthorized user.")
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


def admin_required(func: Callable):
    """Decorator ensuring that only administrative Telegram users can trigger server/management commands."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user or not is_admin(user.id):
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Admin privileges required for this command.")
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


@auth_required
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a rich introduction card for Aura Hub."""
    msg = (
        "🎵 *Aura Hub — Navidrome Management & Audio Pipeline*\n\n"
        "✨ *Features Active:*\n"
        "• *Music Request Queue:* `/request <query or link>` to queue music for ingestion\n"
        "• *Visual Cover Cards:* High-res album covers sent right to chat\n"
        "• *Live Stage Updates:* Real-time step-by-step progress `[1/4]` ➔ `[4/4]`\n"
        "• *AcoustID & MusicBrainz:* Precise acoustic waveforms & genre detection\n"
        "• *Duration & Phonetic Alignment:* Eliminates scrambled track names\n"
        "• *LRCLIB Synced Lyrics:* Automatic `.lrc` companion files for karaoke\n"
        "• *Navidrome Subsonic Integration:* Instant `/rescan` library synchronization\n"
        "• *User Management:* `/users` manage Navidrome accounts & passwords\n"
        "• *Interactive Library Manager:* `/retag` and `/delete` albums visually\n\n"
        "Send any Spotify/YouTube link, or type `/request <name>` to begin!"
    )
    await update.effective_message.reply_markdown(msg)


@auth_required
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays command syntax and usage instructions."""
    help_text = (
        "📖 *Aura Hub Usage Guide:*\n\n"
        "*1. Request Music (All Users):*\n"
        "`/request <song, album or link>` — submit track to the ingestion queue\n"
        "Pasting any Spotify or YouTube link into chat also automatically queues a request!\n\n"
        "*2. Quick Search:*\n"
        "`/search <track or album>` — search YouTube with interactive buttons\n\n"
        "*3. Direct Ingestion (Admins):*\n"
        "Pasting links downloads and tags immediately.\n"
        "`/download <music_url>` — immediate download & tag\n"
        "`/genius <music_url> | <genius_url>` — explicit Genius match\n\n"
        "*4. Interactive Library Maintenance (Admins):*\n"
        "`/retag` — browse folders to refresh ID3 tags and `.lrc` lyrics\n"
        "`/delete` — browse folders to permanently remove albums\n\n"
        "*5. Navidrome Server Management:*\n"
        "`/nowplaying` (or `/np`) — view active playback sessions across users\n"
        "`/rescan` — trigger an instant Subsonic library scan (Admins)\n"
        "`/scanstatus` — view ongoing Navidrome scan metrics\n"
        "`/users` — manage Navidrome user accounts, passwords & roles (Admins)\n"
        "`/requests` — view pending ingestion queue (Admins)\n\n"
        "*6. System & Storage:*\n"
        "`/storage` or `/disk` — view disk metrics and indexed files\n"
        "`/status` — check bot, tool, and Navidrome health\n\n"
        "*7. Web Dashboard (Mini App):*\n"
        "`/hub` — open the interactive Telegram Mini App dashboard"
    )
    await update.effective_message.reply_markdown(help_text)


@auth_required
async def hub_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Provides direct access button to the Aura Hub Telegram Mini App."""
    if not update.effective_message:
        return

    if not config.WEBAPP_EXTERNAL_URL:
        await update.effective_message.reply_markdown(
            "⚠️ *Mini App URL is not configured.*\n\n"
            "Please configure `WEBAPP_EXTERNAL_URL` in your `.env` file (e.g. `https://your-domain.duckdns.org`)."
        )
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⚡ Open Aura Hub Dashboard",
                    web_app=WebAppInfo(url=config.WEBAPP_EXTERNAL_URL),
                )
            ]
        ]
    )
    await update.effective_message.reply_markdown(
        "🚀 *Aura Hub Dashboard*\n\n"
        "Tap the button below to launch the Telegram Mini App for live streams, user management, and server actions:",
        reply_markup=keyboard,
    )


@auth_required
async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Performs end-to-end health checks on the bot, tools, and Navidrome server."""
    status_msg = await update.effective_message.reply_text("🔍 Checking system and services...")

    # Check Navidrome connectivity
    if navidrome_client.is_configured():
        navi_res = navidrome_client.ping()
        navi_status = f"🟢 {navi_res.get('message')}" if navi_res.get("ok") else f"🔴 {navi_res.get('message')}"
    else:
        navi_status = "⚠️ Navidrome credentials not configured in config.py."

    # Diagnostics for external binaries
    diag_summary = get_system_diagnostic_summary()

    msg = (
        "🟢 *Aura Hub is Online and Ready*\n\n"
        "📡 *Navidrome Subsonic API:*\n"
        f"{navi_status}\n\n"
        "🔧 *Host Environment:*\n"
        f"{diag_summary}"
    )
    await status_msg.edit_text(msg, parse_mode="Markdown")


# Router export
router = [
    CommandHandler("start", start_handler),
    CommandHandler("help", help_handler),
    CommandHandler("hub", hub_handler),
    CommandHandler("status", status_handler),
]

