"""Music request and ingestion queue handlers for Aura Hub.

Allows standard authorized users to request songs/albums/links,
and delivers approval cards to administrators with interactive Approve/Reject actions.
"""

import asyncio
import io
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import config
from handlers.common import admin_required, auth_required, is_admin
from services.downloader import executor, run_pipeline, run_youtube_search
from utils.helpers import resolve_fallback_genre
from services.navidrome import navidrome_client
from services.requests import create_request as persist_request, update_request_status as persist_status

logger = logging.getLogger(__name__)

# In-memory queue storing pending and handled requests
# Format:
# req_id -> {
#   "id": str,
#   "user_id": int,
#   "chat_id": int,
#   "user_display": str,
#   "query": str,
#   "display_title": str,
#   "timestamp": float,
#   "status": "pending" | "processing" | "completed" | "rejected" | "failed",
#   "approved_by": Optional[str],
#   "admin_messages": List[Tuple[int, int]],  # (chat_id, message_id)
# }
pending_requests: Dict[str, dict] = {}


def _safe_md(text: str) -> str:
    """Escapes backticks so text can be safely enclosed in markdown code formatting."""
    return str(text).replace("`", "'")


async def submit_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query_or_link: str,
    custom_title: Optional[str] = None,
) -> str:
    """Queues a music request and dispatches approval cards to all server administrators.

    Returns the generated request ID.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return ""

    req_id = secrets.token_hex(4)
    target = query_or_link.strip()
    display_title = custom_title.strip() if custom_title else target

    username_part = f"@{user.username}" if user.username else (user.first_name or f"User {user.id}")

    req_data = {
        "id": req_id,
        "user_id": user.id,
        "chat_id": chat.id,
        "user_display": username_part,
        "query": target,
        "display_title": display_title,
        "timestamp": time.time(),
        "status": "pending",
        "approved_by": None,
        "admin_messages": [],
    }
    pending_requests[req_id] = req_data
    try:
        persist_request(user.id, username_part, target, req_id=req_id)
    except Exception as e:
        logger.warning(f"Failed to persist request {req_id}: {e}")

    # Send receipt confirmation to the requester
    receipt_text = (
        "📥 *Music Request Queued!*\n\n"
        f"• *Request ID:* `{_safe_md(req_id)}`\n"
        f"• *Item:* `{_safe_md(display_title)}`\n\n"
        "Your request has been dispatched to server administrators for review.\n"
        "You will receive an automated notification as soon as it is approved and ingested!"
    )

    if update.effective_message:
        await update.effective_message.reply_markdown(receipt_text)
    else:
        await context.bot.send_message(
            chat_id=chat.id,
            text=receipt_text,
            parse_mode="Markdown",
        )

    # Build admin approval card
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve & Ingest", callback_data=f"req_app:{req_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"req_rej:{req_id}"),
            ]
        ]
    )

    admin_card = (
        "📬 *New Ingestion Request*\n\n"
        f"• *Request ID:* `{_safe_md(req_id)}`\n"
        f"• *Requested by:* `{_safe_md(username_part)}` (`{user.id}`)\n"
        f"• *Target:* `{_safe_md(display_title)}`\n\n"
        "Tap below to approve ingestion or reject this request:"
    )

    # Deliver card to all configured administrators
    for admin_id in config.ADMIN_USER_IDS:
        try:
            sent_msg = await context.bot.send_message(
                chat_id=admin_id,
                text=admin_card,
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            req_data["admin_messages"].append((admin_id, sent_msg.message_id))
        except Exception as e:
            logger.warning(f"Could not deliver approval card to admin {admin_id}: {e}")

    return req_id


@auth_required
async def request_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /request command to queue a track, album, or URL for ingestion."""
    if not update.effective_message or not update.effective_message.text:
        return

    raw_args = update.effective_message.text.partition(" ")[2].strip()
    if not raw_args:
        guide = (
            "💡 *How to Request Music:*\n\n"
            "Use `/request <name or link>` to queue music for admin approval.\n\n"
            "*Examples:*\n"
            "• `/request Amr Diab - Tamally Maak`\n"
            "• `/request https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj`\n"
            "• `/request https://www.youtube.com/watch?v=...`\n\n"
            "You can also simply paste any YouTube or Spotify link directly into the chat!"
        )
        await update.effective_message.reply_markdown(guide)
        return

    await submit_request(update, context, raw_args)


async def approve_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles admin approval of an ingestion request."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = update.effective_user
    if not user or not is_admin(user.id):
        await query.answer("⛔ Admin privileges required.", show_alert=True)
        return

    req_id = query.data.split(":", 1)[1] if ":" in query.data else ""
    req = pending_requests.get(req_id)
    if not req:
        await query.edit_message_text("⚠️ Request expired or not found in queue.")
        return

    if req["status"] != "pending":
        await query.answer(f"This request has already been handled ({req['status']}).", show_alert=True)
        return

    admin_name = user.first_name or f"Admin {user.id}"
    req["status"] = "processing"
    req["approved_by"] = admin_name
    persist_status(req_id, "DOWNLOADING", admin_name=admin_name)

    # Update approving admin message
    await query.edit_message_text(
        f"⏳ `[1/4]` *Ingestion Approved by {admin_name}*\n\n"
        f"• *Request ID:* `{_safe_md(req_id)}`\n"
        f"• *Target:* `{_safe_md(req['display_title'])}`\n"
        "• *Status:* Initializing download & tagging...",
        parse_mode="Markdown",
    )

    # Update other admins' cards so they know this was handled
    for admin_id, msg_id in req.get("admin_messages", []):
        if admin_id != query.from_user.id or msg_id != query.message.message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=admin_id,
                    message_id=msg_id,
                    text=(
                        f"ℹ️ *Request Handled*\n\n"
                        f"• *Request ID:* `{_safe_md(req_id)}`\n"
                        f"• *Item:* `{_safe_md(req['display_title'])}`\n"
                        f"• Approved by: *{admin_name}*",
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    # Notify original requester that request was approved
    try:
        await context.bot.send_message(
            chat_id=req["chat_id"],
            text=(
                f"🎉 *Request Approved!*\n\n"
                f"Your request for `{_safe_md(req['display_title'])}` was approved by an administrator.\n"
                "The audio pipeline is now downloading and tagging it. You'll be notified once it's live!"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"Failed to notify requester {req['chat_id']}: {e}")

    # Launch ingestion pipeline via background executor
    loop = asyncio.get_running_loop()

    def sync_admin_updater(text: str):
        asyncio.run_coroutine_threadsafe(
            query.edit_message_text(text, parse_mode="Markdown"), loop
        )

    try:
        media_url = req["query"]
        custom_title = req.get("display_title")

        # If input is a search query rather than a direct URL, resolve top YouTube result
        if not re.match(r"^https?://", media_url.strip()):
            sync_admin_updater(f"🔎 `[1/4]` *Searching YouTube for:* `{_safe_md(media_url)}`...")
            search_results = await loop.run_in_executor(
                executor, run_youtube_search, media_url, 1
            )
            if not search_results:
                req["status"] = "failed"
                await query.edit_message_text(
                    f"❌ *Ingestion Failed:*\nNo YouTube results found for `{_safe_md(media_url)}`.",
                    parse_mode="Markdown",
                )
                try:
                    await context.bot.send_message(
                        chat_id=req["chat_id"],
                        text=f"❌ Ingestion failed: No media found matching `{_safe_md(media_url)}`.",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
                return

            media_url = search_results[0]["url"]
            custom_title = search_results[0].get("title", custom_title)

        target_folder, meta = await loop.run_in_executor(
            executor, run_pipeline, media_url, "", sync_admin_updater
        )

        req["status"] = "completed"
        persist_status(req_id, "COMPLETED", admin_name=admin_name)
        folder_path = Path(target_folder)
        audio_count = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS])
        lrc_count = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".lrc"])

        album_title = meta.get("album", "Album")
        artist = meta.get("artist", "Artist")
        year = f" ({meta.get('year')})" if meta.get("year") else ""
        genre = resolve_fallback_genre(artist, album_title, meta.get("genre"))

        track_summary = f"{custom_title}\n" if custom_title and custom_title != album_title else ""

        rescan_note = ""
        if navidrome_client.is_configured():
            scan_res = navidrome_client.start_scan()
            if scan_res.get("ok"):
                rescan_note = "\n🔄 _Navidrome library scan initiated automatically._"

        admin_summary = (
            "✅ *Request Ingested & Indexed!*\n\n"
            f"• *Request ID:* `{_safe_md(req_id)}`\n"
            f"• *Requester:* `{_safe_md(req['user_display'])}`\n"
            f"• *Approved by:* {admin_name}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💿 *{album_title}*{year}\n"
            f"👤 *{artist}*\n"
            f"🏷️ `{genre}`\n"
            f"{track_summary}"
            f"✓ *Tracks:* {audio_count} audio tracks tagged\n"
            f"✓ *Synced Lyrics:* {lrc_count} `.lrc` files attached\n"
            f"📂 *Location:* `{folder_path.name}`"
            f"{rescan_note}"
        )

        cover_bytes = meta.get("cover_bytes")
        if cover_bytes:
            try:
                await query.delete_message()
                await query.message.chat.send_photo(
                    photo=io.BytesIO(cover_bytes),
                    caption=admin_summary,
                    parse_mode="Markdown",
                )
            except Exception:
                await query.edit_message_text(admin_summary, parse_mode="Markdown")
        else:
            await query.edit_message_text(admin_summary, parse_mode="Markdown")

        # Notify requester of completion
        requester_card = (
            "🎶 *Your Requested Music is Ready!* 🎧\n\n"
            f"💿 *{album_title}*{year}\n"
            f"👤 *{artist}*\n"
            f"🏷️ `{genre}`\n"
            f"{track_summary}"
            f"✓ *Tracks:* {mp3_count} MP3s tagged\n"
            f"✓ *Synced Lyrics:* {lrc_count} `.lrc` attached\n\n"
            "✨ *Now live and ready in Navidrome & Symfonium!*"
        )
        try:
            if cover_bytes:
                try:
                    await context.bot.send_photo(
                        chat_id=req["chat_id"],
                        photo=io.BytesIO(cover_bytes),
                        caption=requester_card,
                        parse_mode="Markdown",
                    )
                except Exception:
                    await context.bot.send_message(
                        chat_id=req["chat_id"],
                        text=requester_card,
                        parse_mode="Markdown",
                    )
            else:
                await context.bot.send_message(
                    chat_id=req["chat_id"],
                    text=requester_card,
                    parse_mode="Markdown",
                )
        except Exception as e:
            logger.warning(f"Could not deliver completion card to requester {req['chat_id']}: {e}")

    except Exception as e:
        logger.exception(f"Ingestion pipeline failed for request {req_id}")
        req["status"] = "failed"
        persist_status(req_id, "FAILED", error=str(e))
        await query.edit_message_text(
            f"❌ *Ingestion Failed for Request {_safe_md(req_id)}:*\n`{_safe_md(str(e))}`",
            parse_mode="Markdown",
        )
        try:
            await context.bot.send_message(
                chat_id=req["chat_id"],
                text=(
                    f"⚠️ *Ingestion Issue:*\n\n"
                    f"There was a problem ingesting your request for `{_safe_md(req['display_title'])}`:\n`{_safe_md(str(e))}`"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass


async def reject_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles admin rejection of an ingestion request."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = update.effective_user
    if not user or not is_admin(user.id):
        await query.answer("⛔ Admin privileges required.", show_alert=True)
        return

    req_id = query.data.split(":", 1)[1] if ":" in query.data else ""
    req = pending_requests.get(req_id)
    if not req:
        await query.edit_message_text("⚠️ Request expired or not found in queue.")
        return

    if req["status"] != "pending":
        await query.answer(f"This request has already been handled ({req['status']}).", show_alert=True)
        return

    req["status"] = "rejected"
    admin_name = user.first_name or f"Admin {user.id}"
    persist_status(req_id, "REJECTED", admin_name=admin_name)

    # Update admin card
    await query.edit_message_text(
        "❌ *Request Rejected*\n\n"
        f"• *Request ID:* `{_safe_md(req_id)}`\n"
        f"• *Requester:* `{_safe_md(req['user_display'])}`\n"
        f"• *Target:* `{_safe_md(req['display_title'])}`\n"
        f"• *Rejected by:* {admin_name}",
        parse_mode="Markdown",
    )

    # Update other admins' cards
    for admin_id, msg_id in req.get("admin_messages", []):
        if admin_id != query.from_user.id or msg_id != query.message.message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=admin_id,
                    message_id=msg_id,
                    text=(
                        f"❌ *Request Rejected*\n\n"
                        f"• *Request ID:* `{_safe_md(req_id)}`\n"
                        f"• *Item:* `{_safe_md(req['display_title'])}`\n"
                        f"• Rejected by: *{admin_name}*",
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    # Send rejection notification to original requester
    try:
        await context.bot.send_message(
            chat_id=req["chat_id"],
            text=(
                "❌ *Request Not Approved*\n\n"
                f"Your request for `{_safe_md(req['display_title'])}` was reviewed and declined by an administrator."
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"Could not notify requester {req['chat_id']}: {e}")


@admin_required
async def list_requests_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the list of pending and recent ingestion requests to administrators."""
    if not update.effective_message:
        return

    if not pending_requests:
        await update.effective_message.reply_markdown("📭 *Ingestion Queue is empty.* No requests recorded.")
        return

    lines = ["📋 *Ingestion Queue:*", ""]
    for req_id, req in list(pending_requests.items())[-10:]:
        status_icon = {
            "pending": "⏳ Pending",
            "processing": "🔄 Ingesting",
            "completed": "✅ Live",
            "rejected": "❌ Rejected",
            "failed": "⚠️ Failed",
        }.get(req["status"], req["status"])

        lines.append(
            f"• `{_safe_md(req_id)}` | {status_icon}\n"
            f"  Item: `{_safe_md(req['display_title'])}`\n"
            f"  By: `{_safe_md(req['user_display'])}`"
        )

    await update.effective_message.reply_markdown("\n".join(lines))


# Router export
router = [
    CommandHandler("request", request_command_handler),
    CommandHandler("requests", list_requests_handler),
    CallbackQueryHandler(approve_callback_handler, pattern=r"^req_app:"),
    CallbackQueryHandler(reject_callback_handler, pattern=r"^req_rej:"),
]
