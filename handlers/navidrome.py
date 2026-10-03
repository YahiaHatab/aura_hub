"""Navidrome server management handlers: /rescan and /scanstatus."""

import logging
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from handlers.common import auth_required
from services.navidrome import navidrome_client

logger = logging.getLogger(__name__)


@auth_required
async def rescan_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggers an instant Navidrome Subsonic library scan (/rest/startScan)."""
    if not update.effective_message:
        return

    if not navidrome_client.is_configured():
        await update.effective_message.reply_markdown(
            "⚠️ *Navidrome is not configured.*\n\n"
            "Please configure `NAVIDROME_USER` and `NAVIDROME_PASS` in `config.py` or `.env`."
        )
        return

    msg = await update.effective_message.reply_text("🔄 Contacting Navidrome to initiate scan...")
    result = navidrome_client.start_scan()

    if result.get("ok"):
        count = result.get("count", 0)
        await msg.edit_text(
            "🔄 *Navidrome Library Scan Initiated!*\n\n"
            f"• *Status:* Scanning in progress...\n"
            f"• *Indexed so far:* `{count}` tracks\n\n"
            "Use `/scanstatus` to monitor progress.",
            parse_mode="Markdown",
        )
    else:
        err = result.get("message", "Unknown error")
        await msg.edit_text(f"❌ *Failed to trigger Navidrome scan:*\n`{err}`", parse_mode="Markdown")


@auth_required
async def scan_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retrieves current scan progress from Navidrome (/rest/getScanStatus)."""
    if not update.effective_message:
        return

    if not navidrome_client.is_configured():
        await update.effective_message.reply_markdown(
            "⚠️ *Navidrome is not configured.*\n\n"
            "Please configure `NAVIDROME_USER` and `NAVIDROME_PASS` in `config.py` or `.env`."
        )
        return

    result = navidrome_client.get_scan_status()
    if result.get("ok"):
        scanning = result.get("scanning", False)
        count = result.get("count", 0)
        state_icon = "🔄" if scanning else "✅"
        state_text = "Scan actively running" if scanning else "Idle (Scan Complete)"
        await update.effective_message.reply_markdown(
            f"{state_icon} *Navidrome Scan Status:*\n\n"
            f"• *State:* `{state_text}`\n"
            f"• *Indexed Tracks:* `{count}`\n\n"
            + ("_New files are currently being processed._" if scanning else "_All files up to date._")
        )
    else:
        err = result.get("message", "Unknown error")
        await update.effective_message.reply_markdown(
            f"❌ *Could not get scan status:*\n`{err}`"
        )


# Router export
router = [
    CommandHandler("rescan", rescan_handler),
    CommandHandler("scanstatus", scan_status_handler),
]
