"""Library maintenance handlers: /retag and /delete with interactive pagination and confirmations."""

import asyncio
import io
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import config
from handlers.common import admin_required, is_admin
from services.downloader import executor, run_retag_folder
from services.navidrome import navidrome_client
from services.system import delete_album_folder, get_album_folders
from utils.keyboards import build_confirmation_keyboard, build_folder_keyboard

logger = logging.getLogger(__name__)


@admin_required
async def retag_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Presents a paginated folder browser to repair ID3 tags and fetch synced lyrics."""
    folders = get_album_folders()
    if not folders:
        await update.effective_message.reply_text(
            f"📁 No album folders found in `{config.BASE_DOWNLOAD_DIR}`.",
            parse_mode="Markdown",
        )
        return

    context.user_data["cached_folders"] = folders
    reply_markup = build_folder_keyboard(folders, prefix="retag", page=0)
    await update.effective_message.reply_text(
        "📁 *Select an album folder to retag via MusicBrainz & fetch synced lyrics:*",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


@admin_required
async def delete_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Presents a paginated folder browser to delete an album from the server."""
    folders = get_album_folders()
    if not folders:
        await update.effective_message.reply_text(
            f"📁 No album folders found in `{config.BASE_DOWNLOAD_DIR}`.",
            parse_mode="Markdown",
        )
        return

    context.user_data["cached_folders"] = folders
    reply_markup = build_folder_keyboard(folders, prefix="del", page=0)
    await update.effective_message.reply_text(
        "🗑️ *Select an album folder to permanently remove:*",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def library_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles pagination, selection, and confirmation for /retag and /delete."""
    query = update.callback_query
    if not query or not query.from_user or not is_admin(query.from_user.id):
        if query:
            await query.answer("⛔ Admin privileges required.", show_alert=True)
        return
    await query.answer()

    data = query.data or ""

    if data == "noop":
        return

    if data == "folder_cancel":
        await query.edit_message_text("❌ Action cancelled.")
        return

    # ---------------- RETAG CALLBACKS ----------------
    if data.startswith("retag_page:"):
        folders = context.user_data.get("cached_folders") or get_album_folders()
        page = int(data.split(":")[1])
        reply_markup = build_folder_keyboard(folders, prefix="retag", page=page)
        await query.edit_message_reply_markup(reply_markup=reply_markup)
        return

    if data.startswith("retag_sel:"):
        folders = context.user_data.get("cached_folders") or get_album_folders()
        idx = int(data.split(":")[1])
        if idx >= len(folders):
            await query.edit_message_text("⚠️ Selection expired. Run `/retag` again.")
            return

        chosen_rel_path = folders[idx]
        target_abs_path = config.BASE_DOWNLOAD_DIR / chosen_rel_path

        await query.edit_message_text(
            f"⏳ `[1/4]` *Inspecting audio files & durations for:*\n`{chosen_rel_path}`...",
            parse_mode="Markdown",
        )

        loop = asyncio.get_running_loop()

        def sync_retag_updater(text: str):
            asyncio.run_coroutine_threadsafe(
                query.edit_message_text(text, parse_mode="Markdown"), loop
            )

        try:
            target_folder, meta = await loop.run_in_executor(
                executor, run_retag_folder, target_abs_path, "", sync_retag_updater
            )
            folder_path = Path(target_folder)
            audio_count = len([f for f in folder_path.iterdir() if f.suffix.lower() in (".mp3", ".opus")])
            lrc_count = len([f for f in folder_path.iterdir() if f.suffix.lower() == ".lrc"])

            album_title = meta.get("album", "Album")
            artist = meta.get("artist", "Artist")
            year = f" ({meta.get('year')})" if meta.get("year") else ""
            genre = meta.get("genre", "Music")

            try:
                display_rel_path = folder_path.relative_to(config.BASE_DOWNLOAD_DIR).as_posix()
            except ValueError:
                display_rel_path = folder_path.name

            caption = (
                f"🏷️ *Retagged:* *{album_title}*{year}\n"
                f"👤 *{artist}*\n"
                f"🏷️ `{genre}`\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"✓ *Tracks:* {audio_count} audio files refreshed\n"
                f"✓ *Synced Lyrics:* {lrc_count} `.lrc` files active\n"
                f"📂 *Location:* `{display_rel_path}`"
            )
            if display_rel_path != chosen_rel_path:
                caption += f"\n📁 _Unified folder from `{chosen_rel_path}`_"

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
                    logger.warning(f"Could not send photo card for retag: {pe}")

            await query.edit_message_text(caption, parse_mode="Markdown")

        except Exception as e:
            logger.exception("Interactive retag failed")
            await query.edit_message_text(f"❌ *Error during retag:*\n`{e}`", parse_mode="Markdown")
        return

    # ---------------- DELETE CALLBACKS ----------------
    if data.startswith("del_page:"):
        folders = context.user_data.get("cached_folders") or get_album_folders()
        page = int(data.split(":")[1])
        reply_markup = build_folder_keyboard(folders, prefix="del", page=page)
        await query.edit_message_reply_markup(reply_markup=reply_markup)
        return

    if data.startswith("del_sel:"):
        folders = context.user_data.get("cached_folders") or get_album_folders()
        idx = int(data.split(":")[1])
        if idx >= len(folders):
            await query.edit_message_text("⚠️ Selection expired. Run `/delete` again.")
            return

        chosen_rel_path = folders[idx]
        confirm_markup = build_confirmation_keyboard(confirm_callback=f"del_confirm:{idx}")

        await query.edit_message_text(
            "⚠️ *Are you sure you want to remove this album?*\n\n"
            f"📂 `{chosen_rel_path}`\n\n"
            "_This will delete all MP3s and lyrics inside this directory!_",
            reply_markup=confirm_markup,
            parse_mode="Markdown",
        )
        return

    if data.startswith("del_confirm:"):
        folders = context.user_data.get("cached_folders") or get_album_folders()
        idx = int(data.split(":")[1])
        if idx >= len(folders):
            await query.edit_message_text("⚠️ Selection expired. Run `/delete` again.")
            return

        chosen_rel_path = folders[idx]
        success, message = delete_album_folder(chosen_rel_path)

        if success:
            response_text = f"🗑️ *Deleted Successfully:*\n`{chosen_rel_path}`\n\n_Drive space has been freed._"
            if navidrome_client.is_configured():
                scan_res = navidrome_client.start_scan()
                if scan_res.get("ok"):
                    response_text += "\n🔄 _Navidrome rescan initiated to update library._"
            await query.edit_message_text(response_text, parse_mode="Markdown")
        else:
            await query.edit_message_text(f"❌ *Error deleting folder:*\n`{message}`", parse_mode="Markdown")
        return


# Router export
router = [
    CommandHandler("retag", retag_handler),
    CommandHandler("delete", delete_handler),
    CommandHandler("remove", delete_handler),
    CallbackQueryHandler(
        library_callback_handler,
        pattern=r"^(retag_page:|retag_sel:|del_page:|del_sel:|del_confirm:|folder_cancel|noop)",
    ),
]
