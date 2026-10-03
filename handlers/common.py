"""Common handlers: authentication, /start, /help, and /status."""

import functools
import logging
from typing import Callable

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

import config
from services.navidrome import navidrome_client
from services.system import get_system_diagnostic_summary

logger = logging.getLogger(__name__)


def is_authorized(user_id: int) -> bool:
    """Checks whether the user ID is permitted to execute commands."""
    return not config.ALLOWED_USER_IDS or user_id in config.ALLOWED_USER_IDS


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


@auth_required
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a rich introduction card for Aura Hub."""
    msg = (
        "🎵 *Aura Hub — Navidrome Management & Audio Pipeline*\n\n"
        "✨ *Features Active:*\n"
        "• *Visual Cover Cards:* High-res album covers sent right to chat\n"
        "• *Live Stage Updates:* Real-time step-by-step progress `[1/4]` ➔ `[4/4]`\n"
        "• *AcoustID & MusicBrainz:* Precise acoustic waveforms & genre detection\n"
        "• *Duration & Phonetic Alignment:* Eliminates scrambled track names\n"
        "• *LRCLIB Synced Lyrics:* Automatic `.lrc` companion files for karaoke\n"
        "• *Navidrome Subsonic Integration:* Instant `/rescan` library synchronization\n"
        "• *Interactive Library Manager:* `/retag` and `/delete` albums visually\n\n"
        "Send any Spotify/YouTube link, or type `/search <name>` to begin!"
    )
    await update.effective_message.reply_markdown(msg)


@auth_required
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays command syntax and usage instructions."""
    help_text = (
        "📖 *Aura Hub Usage Guide:*\n\n"
        "*1. Quick Search:*\n"
        "`/search <track or album>` — search YouTube with interactive buttons\n\n"
        "*2. Paste Links Directly:*\n"
        "Simply send any Spotify or YouTube URL to automatically download, tag, and generate `.lrc` lyrics.\n\n"
        "*3. Explicit Genius Tagging:*\n"
        "`/download <music_url> | <genius_url>`\n"
        "`/genius <music_url> | <genius_url>`\n\n"
        "*4. Interactive Library Maintenance:*\n"
        "`/retag` — browse folders to refresh ID3 tags and `.lrc` lyrics\n"
        "`/delete` — browse folders to permanently remove albums\n\n"
        "*5. Navidrome Server Management:*\n"
        "`/rescan` — trigger an instant Subsonic library scan\n"
        "`/scanstatus` — view ongoing Navidrome scan metrics\n\n"
        "*6. System & Storage:*\n"
        "`/storage` or `/disk` — view disk metrics and indexed files\n"
        "`/status` — check bot, tool, and Navidrome health"
    )
    await update.effective_message.reply_markdown(help_text)


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
    CommandHandler("status", status_handler),
]
