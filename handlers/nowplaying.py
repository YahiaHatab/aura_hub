"""Real-time active playback session monitor for Navidrome."""

from datetime import datetime
import logging
from typing import Any, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from handlers.common import auth_required, is_authorized
from services.navidrome import navidrome_client

logger = logging.getLogger(__name__)


def escape_md(text: str) -> str:
    """Escapes Telegram Markdown syntax characters."""
    if not text:
        return ""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def escape_code(text: str) -> str:
    """Escapes backticks for inline markdown code blocks."""
    return str(text).replace("`", "'")


def build_now_playing_response(data: Dict[str, Any]) -> str:
    """Formats Navidrome get_now_playing() data into an informative Telegram card."""
    now_str = datetime.now().strftime("%H:%M:%S")

    if not data.get("ok"):
        err = escape_code(data.get("message", "Unknown error"))
        return (
            "📻 *Navidrome Live Monitor*\n\n"
            f"⚠️ *Failed to query Navidrome:*\n`{err}`\n\n"
            f"_Last checked: {now_str}_"
        )

    entries = data.get("entries", [])
    if not entries:
        return (
            "📻 *Navidrome Live Monitor*\n\n"
            "💤 *Server is currently idle.*\n"
            "No active playback sessions detected on Navidrome.\n\n"
            f"_Last checked: {now_str}_"
        )

    lines = [
        "📻 *Navidrome Live Monitor*",
        f"🎧 *Active Sessions:* `{len(entries)}` stream(s)",
        "━━━━━━━━━━━━━━━━━━",
    ]

    for entry in entries:
        username = escape_md(entry.get("username", "Unknown User"))
        player = escape_code(entry.get("player", "Unknown Client"))
        title = escape_md(entry.get("title", "Unknown Title"))
        artist = escape_md(entry.get("artist", "Unknown Artist"))
        album = escape_md(entry.get("album", "Unknown Album"))

        bitrate = entry.get("bitrate")
        fmt = entry.get("format", "MP3")
        bitrate_str = f"{bitrate} kbps • {fmt}" if bitrate else fmt

        minutes_ago = entry.get("minutes_ago", 0)
        if minutes_ago == 0:
            elapsed_str = "Active now (< 1m ago)"
        elif minutes_ago == 1:
            elapsed_str = "1 min ago"
        else:
            elapsed_str = f"{minutes_ago} mins ago"

        duration = entry.get("duration", 0)
        duration_str = f" [{duration // 60}:{duration % 60:02d}]" if duration > 0 else ""

        card_block = (
            f"🎧 *{username}* streaming on `{player}`\n"
            f"🎵 *{title}*{duration_str} — {artist}\n"
            f"💿 *Album:* {album}\n"
            f"📊 *Stream:* {bitrate_str}\n"
            f"⏱️ *Status:* {elapsed_str}"
        )
        lines.append(card_block)
        lines.append("━━━━━━━━━━━━━━━━━━")

    lines.append(f"_Last updated: {now_str}_")
    return "\n".join(lines)


def get_refresh_keyboard() -> InlineKeyboardMarkup:
    """Returns the [🔄 Refresh] inline keyboard markup."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Refresh", callback_data="np_refresh")]]
    )


@auth_required
async def nowplaying_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responds with active Navidrome playback streams or idle status."""
    if not update.effective_message:
        return

    if not navidrome_client.is_configured():
        await update.effective_message.reply_markdown(
            "⚠️ *Navidrome is not configured.*\n\n"
            "Please configure `NAVIDROME_USER` and `NAVIDROME_PASS` in `config.py` or `.env`."
        )
        return

    data = navidrome_client.get_now_playing()
    text = build_now_playing_response(data)
    keyboard = get_refresh_keyboard()

    await update.effective_message.reply_markdown(text, reply_markup=keyboard)


async def nowplaying_refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-polls Navidrome getNowPlaying and updates the Telegram card in place."""
    query = update.callback_query
    if not query:
        return

    user = update.effective_user
    if not user or not is_authorized(user.id):
        await query.answer("⛔ Unauthorized user.", show_alert=True)
        return

    await query.answer()

    data = navidrome_client.get_now_playing()
    text = build_now_playing_response(data)
    keyboard = get_refresh_keyboard()

    try:
        await query.edit_message_text(
            text=text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    except Exception as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logger.warning(f"Error editing now playing message: {e}")


# Router export
router = [
    CommandHandler(["nowplaying", "np"], nowplaying_handler),
    CallbackQueryHandler(nowplaying_refresh_callback, pattern=r"^np_refresh$"),
]
