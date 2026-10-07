"""Download handlers: auto-catcher for media links, /download, /genius, and /search."""

import asyncio
import io
import logging
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from handlers.common import admin_required, auth_required, is_admin
from services.downloader import executor, run_pipeline, run_youtube_search
from services.navidrome import navidrome_client
from services.settings import get_quality_preference, parse_quality_flag
from utils.keyboards import build_search_results_keyboard

logger = logging.getLogger(__name__)


async def execute_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    media_url: str,
    genius_input: str = "",
    custom_title: Optional[str] = None,
    quality: Optional[str] = None,
):
    """Executes the complete download and tagging pipeline with live status updates and photo cards."""
    chat = update.effective_chat
    if not chat:
        return

    user = update.effective_user
    user_id = user.id if user else None
    effective_quality = quality or get_quality_preference(user_id)

    status_msg = await chat.send_message(
        f"⏳ `[1/4]` *Initializing download pipeline ({effective_quality.upper()})...*",
        parse_mode="Markdown",
    )

    loop = asyncio.get_running_loop()

    def sync_status_updater(text: str):
        asyncio.run_coroutine_threadsafe(
            status_msg.edit_text(text, parse_mode="Markdown"), loop
        )

    try:
        target_folder, meta = await loop.run_in_executor(
            executor,
            run_pipeline,
            media_url,
            genius_input,
            sync_status_updater,
            effective_quality,
        )

        folder_path = Path(target_folder)
        flac_count = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".flac"])
        opus_count = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".opus"])
        m4a_count = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".m4a"])
        mp3_count = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".mp3"])
        total_audio = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS])
        lrc_count = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".lrc"])

        if flac_count > 0:
            format_tag = f"{flac_count} FLAC (Lossless)"
        elif opus_count > 0:
            format_tag = f"{opus_count} Opus (Native)"
        elif m4a_count > 0:
            format_tag = f"{m4a_count} M4A (AAC)"
        else:
            format_tag = f"{mp3_count} MP3"

        album_title = meta.get("album", "Album")
        artist = meta.get("artist", "Artist")
        year = f" ({meta.get('year')})" if meta.get("year") else ""
        genre = meta.get("genre", "Music")

        track_summary = f"{custom_title}\n" if custom_title else ""
        caption = (
            f"💿 *{album_title}*{year}\n"
            f"👤 *{artist}*\n"
            f"🏷️ `{genre}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{track_summary}"
            f"✓ *Tracks:* {total_audio} tagged ({format_tag})\n"
            f"✓ *Synced Lyrics:* {lrc_count} `.lrc` files attached\n"
            f"📂 *Location:* `{folder_path.name}`\n\n"
            f"✨ *Ready in Symfonium & Navidrome!*"
        )

        # Trigger automatic background Navidrome scan if configured
        if navidrome_client.is_configured():
            scan_res = navidrome_client.start_scan()
            if scan_res.get("ok"):
                caption += "\n🔄 _Navidrome rescan initiated automatically._"

        cover_bytes = meta.get("cover_bytes")
        if cover_bytes:
            try:
                await status_msg.delete()
                await chat.send_photo(
                    photo=io.BytesIO(cover_bytes),
                    caption=caption,
                    parse_mode="Markdown",
                )
                return
            except Exception as pe:
                logger.warning(f"Could not send photo card: {pe}; falling back to text.")

        await status_msg.edit_text(caption, parse_mode="Markdown")

    except Exception as e:
        logger.exception("Download pipeline failed")
        await status_msg.edit_text(f"❌ *Error:*\n`{e}`", parse_mode="Markdown")


@auth_required
async def auto_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Automatically triggers download for admins or queues an ingestion request for standard users."""
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    clean_text, flag_qual = parse_quality_flag(text)
    user = update.effective_user
    chosen_qual = flag_qual or get_quality_preference(user.id if user else None)

    if user and is_admin(user.id):
        await execute_task(update, context, clean_text, "", quality=chosen_qual)
    else:
        from handlers.request import submit_request

        await submit_request(update, context, clean_text)


@auth_required
async def download_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /download command with optional Genius match syntax (URL | GeniusURL) and quality flags.

    Admins download immediately; standard users are routed to the request queue.
    """
    if not update.message or not update.message.text:
        return

    raw_args = update.message.text.partition(" ")[2].strip()
    if not raw_args:
        await update.message.reply_text(
            "Please provide a link. Example:\n`/download <url> [--flac|--opus|--mp3]`",
            parse_mode="Markdown",
        )
        return

    clean_args, flag_qual = parse_quality_flag(raw_args)
    user = update.effective_user
    if user and not is_admin(user.id):
        from handlers.request import submit_request

        await submit_request(update, context, clean_args)
        return

    genius_input = ""
    if "|" in clean_args:
        parts = clean_args.split("|", 1)
        media_url = parts[0].strip()
        genius_input = parts[1].strip()
    else:
        media_url = clean_args

    chosen_qual = flag_qual or get_quality_preference(user.id if user else None)
    await execute_task(update, context, media_url, genius_input, quality=chosen_qual)


@admin_required
async def genius_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /genius command requiring explicit <media_url> | <genius_url> pairing and optional quality flag."""
    if not update.message or not update.message.text:
        return

    raw_args = update.message.text.partition(" ")[2].strip()
    if not raw_args or "|" not in raw_args:
        await update.message.reply_text(
            "Format required:\n`/genius <music_url> | <genius_url> [--flac|--opus|--mp3]`",
            parse_mode="Markdown",
        )
        return

    clean_args, flag_qual = parse_quality_flag(raw_args)
    parts = clean_args.split("|", 1)
    media_url = parts[0].strip()
    genius_input = parts[1].strip()

    user = update.effective_user
    chosen_qual = flag_qual or get_quality_preference(user.id if user else None)
    await execute_task(update, context, media_url, genius_input, quality=chosen_qual)


@auth_required
async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Performs YouTube search and presents interactive download buttons."""
    if not update.message or not update.message.text:
        return

    query = update.message.text.partition(" ")[2].strip()
    if not query:
        await update.message.reply_text("Please provide a search query. Example:\n`/search Amr Diab`")
        return

    status_msg = await update.message.reply_text(
        f"🔎 *Searching for:* `{query}`...", parse_mode="Markdown"
    )

    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(executor, run_youtube_search, query)
        if not results:
            await status_msg.edit_text("❌ No results found on YouTube.")
            return

        context.user_data["search_results"] = results
        keyboard = build_search_results_keyboard(results, prefix="yt_dl")

        await status_msg.edit_text(
            f"🎯 *Search Results for:* `{query}`\n*Tap a track to download & tag:*",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("Search query failed")
        await status_msg.edit_text(f"❌ *Search error:*\n`{e}`", parse_mode="Markdown")


async def search_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles selection of a search result button."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    if not data.startswith("yt_dl:"):
        return

    results = context.user_data.get("search_results", [])
    idx = int(data.split(":")[1])
    if idx >= len(results):
        await query.edit_message_text("⚠️ Search expired. Run `/search` again.")
        return

    chosen_item = results[idx]
    chosen_url = chosen_item["url"]
    chosen_title = chosen_item.get("title", "")

    user = update.effective_user
    if user and not is_admin(user.id):
        from handlers.request import submit_request

        await query.edit_message_text(
            f"📥 *Queueing request for:* `{chosen_title}`...",
            parse_mode="Markdown",
        )
        await submit_request(update, context, chosen_url, custom_title=chosen_title)
        return

    chosen_qual = get_quality_preference(user.id if user else None)
    await query.edit_message_text(
        f"⏳ `[1/4]` *Starting download ({chosen_qual.upper()}) for:*\n`{chosen_title}`...",
        parse_mode="Markdown",
    )

    loop = asyncio.get_running_loop()

    def sync_search_updater(text: str):
        asyncio.run_coroutine_threadsafe(
            query.edit_message_text(text, parse_mode="Markdown"), loop
        )

    try:
        target_folder, meta = await loop.run_in_executor(
            executor, run_pipeline, chosen_url, "", sync_search_updater, chosen_qual
        )
        folder_path = Path(target_folder)
        flac_count = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".flac"])
        opus_count = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".opus"])
        m4a_count = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".m4a"])
        mp3_count = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".mp3"])
        total_audio = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS])
        lrc_count = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".lrc"])

        if flac_count > 0:
            fmt_label = f"{flac_count} FLAC (Lossless)"
        elif opus_count > 0:
            fmt_label = f"{opus_count} Opus (Native)"
        elif m4a_count > 0:
            fmt_label = f"{m4a_count} M4A (AAC)"
        else:
            fmt_label = f"{mp3_count} MP3"

        album_title = meta.get("album", "Album")
        artist = meta.get("artist", "Artist")
        year = f" ({meta.get('year')})" if meta.get("year") else ""
        genre = meta.get("genre", "Music")

        caption = (
            f"💿 *{album_title}*{year}\n"
            f"👤 *{artist}*\n"
            f"🏷️ `{genre}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"✓ *Track:* {chosen_title}\n"
            f"✓ *Format:* {fmt_label}\n"
            f"✓ *Synced Lyrics:* {lrc_count} `.lrc` file\n"
            f"📂 *Saved to:* `{folder_path.name}`"
        )

        if navidrome_client.is_configured():
            scan_res = navidrome_client.start_scan()
            if scan_res.get("ok"):
                caption += "\n🔄 _Navidrome rescan initiated automatically._"

        cover_bytes = meta.get("cover_bytes")
        if cover_bytes:
            try:
                await query.delete_message()
                await query.message.chat.send_photo(
                    photo=io.BytesIO(cover_bytes),
                    caption=caption,
                    parse_mode="Markdown",
                )
                return
            except Exception as pe:
                logger.warning(f"Failed to send photo card for search result: {pe}")

        await query.edit_message_text(caption, parse_mode="Markdown")

    except Exception as e:
        logger.exception("Search download failed")
        await query.edit_message_text(f"❌ *Download error:*\n`{e}`", parse_mode="Markdown")


# Router export
router = [
    CommandHandler("download", download_handler),
    CommandHandler("genius", genius_handler),
    CommandHandler("search", search_handler),
    CallbackQueryHandler(search_callback_handler, pattern=r"^yt_dl:"),
    MessageHandler(
        filters.TEXT
        & ~filters.COMMAND
        & filters.Regex(
            r"https?://(?:[\w-]+\.)?(?:spotify\.com|spotify\.link|youtube\.com|youtu\.be|tidal\.com|deezer\.com|qobuz\.com)/\S+"
        ),
        auto_link_handler,
    ),
]
