"""Interactive metadata search & inspection handler for Aura Hub (/metadata and /meta).

Allows searching and browsing candidate releases across MusicBrainz, iTunes,
Deezer, Spotify, and Discogs with full previews, confidence scores, and one-click
album retagging to local library folders.
"""

import asyncio
import io
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import config
from handlers.common import admin_required, is_admin
from services.downloader import executor, run_retag_folder
from services.metadata import (
    MetadataCandidate,
    UnifiedAlbumMetadata,
    search_album_metadata_candidates_async,
)
from services.navidrome import navidrome_client
from services.system import get_album_folders
from utils.keyboards import build_folder_keyboard

logger = logging.getLogger(__name__)


def _format_duration(seconds: float) -> str:
    """Formats seconds into MM:SS string."""
    if not seconds or seconds <= 0:
        return "--:--"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def _build_candidate_card(
    candidate: MetadataCandidate, index: int, total: int
) -> Tuple[str, InlineKeyboardMarkup]:
    """Renders the Markdown summary and interactive navigation buttons for a candidate."""
    alb = candidate.album_data or UnifiedAlbumMetadata()
    source = candidate.source
    conf = int(candidate.confidence_score)

    source_icons = {
        "iTunes": "🍎 iTunes",
        "MusicBrainz": "💿 MusicBrainz",
        "Deezer": "🎧 Deezer",
        "Spotify": "🟢 Spotify",
        "Discogs": "📀 Discogs",
    }
    icon_source = source_icons.get(source, f"🌐 {source}")

    header = f"🏷️ *Metadata Search Results* `({index + 1}/{total})`\n"
    if candidate.is_recommended:
        header += "⭐ *RECOMMENDED MATCH*\n"

    tracks_cnt = alb.total_tracks or len(alb.tracks)
    genre_display = alb.genre or ", ".join(alb.genres[:2]) if alb.genres else "Unknown"
    year_display = f" ({alb.year[:4]})" if alb.year else ""

    text = (
        f"{header}\n"
        f"🎵 *Album:* *{alb.album}*{year_display}\n"
        f"👤 *Artist:* *{alb.artist}*\n"
        f"🏷️ *Genre:* `{genre_display}`\n"
        f"🌐 *Provider:* `{icon_source}` • 📊 *Confidence:* `{conf}%`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
    )

    if alb.producers:
        text += f"🎧 *Producers:* _{', '.join(alb.producers[:2])}_\n"
    if alb.composers:
        text += f"🎼 *Composers:* _{', '.join(alb.composers[:2])}_\n"

    text += f"💿 *Tracklist:* ({tracks_cnt} tracks)\n"
    if alb.tracks:
        for t in alb.tracks[:5]:
            dur_str = _format_duration(t.duration_seconds)
            text += f"`{t.track_number:02d}.` {t.title[:28]} `({dur_str})`\n"
        if len(alb.tracks) > 5:
            text += f"_... and {len(alb.tracks) - 5} more tracks_\n"
    else:
        text += "_Tracklist not returned by provider_\n"

    # Build navigation and action keyboard
    kb = [
        [InlineKeyboardButton("🏷️ Apply to Local Album...", callback_data=f"meta_apply:{index}")],
    ]

    nav_row = []
    if total > 1:
        if index > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"meta_nav:{index - 1}"))
        nav_row.append(InlineKeyboardButton(f"{index + 1}/{total}", callback_data="noop"))
        if index < total - 1:
            nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"meta_nav:{index + 1}"))
    if nav_row:
        kb.append(nav_row)

    kb.append([
        InlineKeyboardButton("📋 Full Tracklist", callback_data=f"meta_tracks:{index}"),
        InlineKeyboardButton("❌ Close", callback_data="meta_close"),
    ])

    return text, InlineKeyboardMarkup(kb)


@admin_required
async def metadata_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Searches multi-source metadata backends for albums and displays interactive candidate cards."""
    if not update.effective_message:
        return

    raw_args = " ".join(context.args).strip() if context.args else ""
    if not raw_args:
        await update.effective_message.reply_text(
            "🏷️ *Metadata Inspector & Aggregator*\n\n"
            "Query multiple backends (MusicBrainz, iTunes, Deezer, Spotify, Discogs) "
            "with confidence scoring and one-click library retagging.\n\n"
            "*Usage:*\n"
            "`/metadata <artist - album>`\n"
            "`/metadata <album title>`\n\n"
            "*Examples:*\n"
            "`/metadata Elissa - Saharna Ya Leil`\n"
            "`/metadata Amr Diab - Kol Hayaty`\n"
            "`/metadata Abbey Road`",
            parse_mode="Markdown",
        )
        return

    # Parse query into artist and album if separator exists
    artist = ""
    album = raw_args
    if " - " in raw_args:
        parts = raw_args.split(" - ", 1)
        artist = parts[0].strip()
        album = parts[1].strip()
    elif "," in raw_args:
        parts = raw_args.split(",", 1)
        artist = parts[0].strip()
        album = parts[1].strip()

    status_msg = await update.effective_message.reply_text(
        f"🔎 *Searching providers for:* `{album}`"
        + (f" by `{artist}`" if artist else "")
        + "...\n_(Querying MusicBrainz, iTunes, Deezer, Spotify)_",
        parse_mode="Markdown",
    )

    try:
        candidates = await search_album_metadata_candidates_async(album, artist)
        if not candidates:
            await status_msg.edit_text(
                f"❌ *No metadata candidates found for:* `{raw_args}`\n"
                "Try checking spelling or providing both artist and album name.",
                parse_mode="Markdown",
            )
            return

        context.user_data["meta_candidates"] = [c.to_dict() for c in candidates]
        context.user_data["meta_query"] = raw_args

        text, markup = _build_candidate_card(candidates[0], 0, len(candidates))
        cover_url = candidates[0].album_data.cover_url if candidates[0].album_data else ""

        if cover_url:
            try:
                await status_msg.delete()
                await update.effective_message.reply_photo(
                    photo=cover_url,
                    caption=text,
                    reply_markup=markup,
                    parse_mode="Markdown",
                )
                return
            except Exception as pe:
                logger.debug(f"Could not send photo directly: {pe}")

        await status_msg.edit_text(text, reply_markup=markup, parse_mode="Markdown")

    except Exception as e:
        logger.exception("Metadata search command failed")
        await status_msg.edit_text(f"❌ *Search Error:* `{e}`", parse_mode="Markdown")


async def metadata_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles candidate pagination, tracklist viewing, and one-click application to albums."""
    query = update.callback_query
    if not query or not query.from_user or not is_admin(query.from_user.id):
        if query:
            await query.answer("⛔ Admin privileges required.", show_alert=True)
        return
    await query.answer()

    data = query.data or ""
    if data == "noop":
        return

    if data == "meta_close":
        try:
            await query.message.delete()
        except Exception:
            await query.edit_message_text("❌ Dismissed.")
        return

    # Navigation between candidates
    if data.startswith("meta_nav:"):
        idx = int(data.split(":")[1])
        raw_candidates = context.user_data.get("meta_candidates", [])
        if not raw_candidates or idx >= len(raw_candidates):
            await query.answer("Search expired. Run /metadata again.", show_alert=True)
            return

        # Reconstruct candidate
        cand_dict = raw_candidates[idx]
        album_meta = (
            UnifiedAlbumMetadata.from_dict(cand_dict["album_data"])
            if cand_dict.get("album_data")
            else None
        )
        cand = MetadataCandidate(
            source=cand_dict["source"],
            confidence_score=cand_dict["confidence_score"],
            is_recommended=cand_dict["is_recommended"],
            album_data=album_meta,
            preview=cand_dict.get("preview", {}),
        )

        text, markup = _build_candidate_card(cand, idx, len(raw_candidates))
        cover_url = album_meta.cover_url if album_meta else ""

        # Update message
        if query.message.photo and cover_url:
            try:
                from telegram import InputMediaPhoto
                await query.edit_message_media(
                    media=InputMediaPhoto(media=cover_url, caption=text, parse_mode="Markdown"),
                    reply_markup=markup,
                )
                return
            except Exception:
                pass

        if query.message.caption is not None:
            await query.edit_message_caption(caption=text, reply_markup=markup, parse_mode="Markdown")
        else:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    # View full tracklist
    if data.startswith("meta_tracks:"):
        idx = int(data.split(":")[1])
        raw_candidates = context.user_data.get("meta_candidates", [])
        if not raw_candidates or idx >= len(raw_candidates):
            await query.answer("Search expired.", show_alert=True)
            return

        cand_dict = raw_candidates[idx]
        album_data = cand_dict.get("album_data", {})
        tracks = album_data.get("tracks", [])

        lines = [f"📋 *Full Tracklist — {album_data.get('album')}* ({len(tracks)} tracks):\n"]
        for t in tracks:
            d_str = _format_duration(t.get("duration_seconds", 0))
            lines.append(f"`{t.get('track_number', 0):02d}.` {t.get('title', '')} `({d_str})`")

        full_text = "\n".join(lines)
        if len(full_text) > 4000:
            full_text = full_text[:3950] + "\n_... [truncated]_"

        kb = [[InlineKeyboardButton("⬅️ Back to Card", callback_data=f"meta_nav:{idx}")]]
        if query.message.caption is not None:
            await query.edit_message_caption(
                caption=full_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                full_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
            )
        return

    # Trigger folder selection to apply chosen candidate
    if data.startswith("meta_apply:"):
        idx = int(data.split(":")[1])
        context.user_data["meta_selected_candidate_idx"] = idx
        folders = get_album_folders()
        if not folders:
            await query.answer("No album folders found in library directory.", show_alert=True)
            return

        context.user_data["cached_folders"] = folders
        reply_markup = build_folder_keyboard(folders, prefix="meta_fld", page=0)
        select_text = (
            "📁 *Select a local album folder to tag with this metadata:*\n"
            "_(Tags, artwork, and .lrc lyrics will be applied to all tracks)_"
        )

        if query.message.caption is not None:
            await query.edit_message_caption(
                caption=select_text, reply_markup=reply_markup, parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                select_text, reply_markup=reply_markup, parse_mode="Markdown"
            )
        return

    # Pagination for folder keyboard in metadata workflow
    if data.startswith("meta_fld_page:"):
        folders = context.user_data.get("cached_folders") or get_album_folders()
        page = int(data.split(":")[1])
        reply_markup = build_folder_keyboard(folders, prefix="meta_fld", page=page)
        await query.edit_message_reply_markup(reply_markup=reply_markup)
        return

    # Execute retagging on selected folder
    if data.startswith("meta_fld_sel:"):
        idx = int(data.split(":")[1])
        folders = context.user_data.get("cached_folders") or get_album_folders()
        if idx >= len(folders):
            await query.edit_message_text("⚠️ Selection expired. Run `/metadata` again.")
            return

        chosen_rel_path = folders[idx]
        target_abs_path = config.BASE_DOWNLOAD_DIR / chosen_rel_path

        cand_idx = context.user_data.get("meta_selected_candidate_idx", 0)
        raw_candidates = context.user_data.get("meta_candidates", [])
        if not raw_candidates or cand_idx >= len(raw_candidates):
            await query.edit_message_text("⚠️ Candidate expired. Run `/metadata` again.")
            return

        chosen_cand_dict = raw_candidates[cand_idx]
        chosen_album = UnifiedAlbumMetadata.from_dict(chosen_cand_dict["album_data"])

        progress_msg = (
            f"⏳ `[1/4]` *Applying metadata from {chosen_cand_dict['source']} to:*\n"
            f"`{chosen_rel_path}`..."
        )
        if query.message.caption is not None:
            await query.edit_message_caption(caption=progress_msg, parse_mode="Markdown")
        else:
            await query.edit_message_text(progress_msg, parse_mode="Markdown")

        loop = asyncio.get_running_loop()

        def sync_meta_updater(text: str):
            asyncio.run_coroutine_threadsafe(
                query.edit_message_text(text, parse_mode="Markdown")
                if query.message.caption is None
                else query.edit_message_caption(caption=text, parse_mode="Markdown"),
                loop,
            )

        try:
            target_folder, meta = await loop.run_in_executor(
                executor,
                run_retag_folder,
                target_abs_path,
                "",
                sync_meta_updater,
                chosen_album,
            )

            folder_path = Path(target_folder)
            audio_count = len([
                f for f in folder_path.iterdir()
                if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS
            ])
            lrc_count = len([
                f for f in folder_path.iterdir()
                if f.is_file() and f.suffix.lower() == ".lrc"
            ])

            album_title = meta.get("album", chosen_album.album)
            artist = meta.get("artist", chosen_album.artist)
            year = f" ({meta.get('year')})" if meta.get("year") else ""
            genre = meta.get("genre", chosen_album.genre or "Arabic Pop")

            try:
                display_rel_path = folder_path.relative_to(config.BASE_DOWNLOAD_DIR).as_posix()
            except ValueError:
                display_rel_path = folder_path.name

            caption = (
                f"🏷️ *Metadata Applied Successfully!*\n"
                f"🎵 *Album:* *{album_title}*{year}\n"
                f"👤 *Artist:* *{artist}*\n"
                f"🏷️ *Genre:* `{genre}`\n"
                f"🌐 *Source:* `{chosen_cand_dict['source']}`\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"✓ *Tracks Tagged:* {audio_count} files\n"
                f"✓ *Synced Lyrics:* {lrc_count} active\n"
                f"📂 *Location:* `{display_rel_path}`"
            )

            if navidrome_client.is_configured():
                scan_res = navidrome_client.start_scan()
                if scan_res.get("ok"):
                    caption += "\n🔄 _Navidrome rescan initiated automatically._"

            cover_bytes = meta.get("cover_bytes") or chosen_album.cover_bytes
            if cover_bytes:
                try:
                    await query.message.delete()
                    await query.message.chat.send_photo(
                        photo=io.BytesIO(cover_bytes),
                        caption=caption,
                        parse_mode="Markdown",
                    )
                    return
                except Exception as pe:
                    logger.debug(f"Could not send photo card: {pe}")

            if query.message.caption is not None:
                await query.edit_message_caption(caption=caption, parse_mode="Markdown")
            else:
                await query.edit_message_text(caption, parse_mode="Markdown")

        except Exception as e:
            logger.exception("Applying metadata failed")
            err_text = f"❌ *Error applying metadata:* `{e}`"
            if query.message.caption is not None:
                await query.edit_message_caption(caption=err_text, parse_mode="Markdown")
            else:
                await query.edit_message_text(err_text, parse_mode="Markdown")


# Handler router exported for main.py registration
router = [
    CommandHandler("metadata", metadata_search_command),
    CommandHandler("meta", metadata_search_command),
    CallbackQueryHandler(
        metadata_callback_handler,
        pattern=r"^(meta_nav:|meta_apply:|meta_tracks:|meta_fld_page:|meta_fld_sel:|meta_close)",
    ),
]
