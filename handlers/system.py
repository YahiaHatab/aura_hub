"""System storage and library metrics handlers: /storage and /disk."""

import logging
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from handlers.common import auth_required
from services.system import get_disk_metrics

logger = logging.getLogger(__name__)


@auth_required
async def storage_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reports disk usage, partition metrics, and count of indexed MP3 & LRC files."""
    if not update.effective_message:
        return

    metrics = get_disk_metrics()
    used_gb = metrics["used_gb"]
    total_gb = metrics["total_gb"]
    free_gb = metrics["free_gb"]
    pct = metrics["pct_used"]
    audio_count = metrics.get("audio_count", metrics.get("mp3_count", 0))
    lrc_count = metrics["lrc_count"]
    base_dir = metrics["base_dir"]

    msg = (
        "💾 *Server Storage & Library Metrics*\n\n"
        f"• *Disk Usage:* `{used_gb:.1f} GB` / `{total_gb:.1f} GB` ({pct:.1f}%)\n"
        f"• *Free Disk Space:* `{free_gb:.1f} GB`\n"
        f"• *Music Directory:* `{base_dir}`\n"
        f"• *Audio Files:* `{audio_count}` audio tracks indexed\n"
        f"• *Synced Lyrics:* `{lrc_count}` `.lrc` companion files"
    )
    await update.effective_message.reply_markdown(msg)


# Router export
router = [
    CommandHandler("storage", storage_handler),
    CommandHandler("disk", storage_handler),
]
