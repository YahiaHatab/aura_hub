"""Download handlers: auto-catcher for media links, /download, /genius, /search,
and post-download interactive metadata source review.
"""

import asyncio
import io
import logging
from pathlib import Path
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from handlers.common import admin_required, auth_required, is_admin
from services.downloader import (
    download_media_staging,
    executor,
    finalize_staged_media,
    run_youtube_search,
)
from services.metadata import (
    MetadataCandidate,
    search_album_metadata_candidates_async,
    search_track_metadata_candidates_async,
)
from services.navidrome import navidrome_client
from services.settings import get_quality_preference, parse_quality_flag
from utils.helpers import resolve_fallback_genre
from utils.keyboards import (
    build_metadata_diff_keyboard,
    build_metadata_empty_keyboard,
    build_metadata_review_keyboard,
    build_search_results_keyboard,
)

logger = logging.getLogger(__name__)

# Active post-download review sessions keyed by session_id
_DOWNLOAD_SESSIONS: Dict[str, Dict[str, Any]] = {}


def _clean_expired_sessions() -> None:
    """Prunes download sessions older than 2 hours."""
    now = time.time()
    expired = [
        sid
        for sid, sess in _DOWNLOAD_SESSIONS.items()
        if now - sess.get("created_at", 0) > 7200
    ]
    for sid in expired:
        sess = _DOWNLOAD_SESSIONS.pop(sid, None)
        if sess:
            stg = sess.get("staging_dir")
            if stg and isinstance(stg, Path) and stg.exists():
                shutil.rmtree(str(stg), ignore_errors=True)


def _format_recommendation_label(candidate: MetadataCandidate) -> str:
    """Generates an informative highlight label for the recommended metadata candidate."""
    p = candidate.preview or {}
    highlights = []
    if p.get("has_cover"):
        highlights.append("Hi-Res Cover")
    if p.get("has_producers") or p.get("has_composers"):
        highlights.append("Full Credits")
    if p.get("has_lyrics"):
        highlights.append("Synced Lyrics")
    if p.get("track_count") and p["track_count"] > 1:
        highlights.append(f"{p['track_count']} Tracks")

    desc = " & ".join(highlights[:2]) if highlights else "Best Match"
    return f"⭐️ *{candidate.source}* (`{int(candidate.confidence_score)}% match` — {desc})"


def _render_metadata_review_message(
    session: Dict[str, Any],
) -> Tuple[str, InlineKeyboardMarkup]:
    """Renders the Markdown review text and interactive inline keyboard."""
    candidates: List[MetadataCandidate] = session.get("candidates", [])
    session_id: str = session["session_id"]
    detected_album: str = session["detected_album"]
    detected_artist: str = session["detected_artist"]
    staging_dir: Path = session["staging_dir"]

    flac_count = len([f for f in staging_dir.iterdir() if f.is_file() and f.suffix.lower() == ".flac"])
    opus_count = len([f for f in staging_dir.iterdir() if f.is_file() and f.suffix.lower() == ".opus"])
    m4a_count = len([f for f in staging_dir.iterdir() if f.is_file() and f.suffix.lower() == ".m4a"])
    mp3_count = len([f for f in staging_dir.iterdir() if f.is_file() and f.suffix.lower() == ".mp3"])
    total_audio = len([f for f in staging_dir.iterdir() if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS])

    if flac_count > 0:
        format_tag = f"{flac_count} FLAC (Lossless)"
    elif opus_count > 0:
        format_tag = f"{opus_count} Opus (Native)"
    elif m4a_count > 0:
        format_tag = f"{m4a_count} M4A (AAC)"
    else:
        format_tag = f"{mp3_count} MP3"

    if candidates:
        rec_cand = next((c for c in candidates if c.is_recommended), candidates[0])
        rec_highlight = _format_recommendation_label(rec_cand)

        cand_lines = []
        for idx, c in enumerate(candidates[:5], 1):
            star = "⭐️ " if c.is_recommended else ""
            p = c.preview or {}
            extras = []
            if p.get("has_cover"):
                extras.append("Hi-Res Cover")
            if p.get("has_producers") or p.get("has_composers"):
                extras.append("Credits")
            if p.get("has_lyrics"):
                extras.append("Lyrics")
            extra_str = f" — {', '.join(extras)}" if extras else ""
            cand_lines.append(
                f"`{idx}.` {star}*{c.source}* (`{int(c.confidence_score)}% match`{extra_str})"
            )

        text = (
            f"📥 *Download Complete — Metadata Review*\n\n"
            f"🎵 *Detected:* *{detected_album}*\n"
            f"👤 *Artist:* *{detected_artist}*\n"
            f"📁 *Files:* {total_audio} audio tracks `({format_tag})`\n\n"
            f"⭐ *Recommended Source:*\n"
            f"{rec_highlight}\n\n"
            f"📊 *Available Metadata Candidates:*\n"
            + "\n".join(cand_lines)
            + "\n\n_Select a preferred source below to apply tags & lyrics, or inspect the diff:_"
        )
        markup = build_metadata_review_keyboard(candidates, session_id)
    else:
        text = (
            f"⚠️ *No High-Confidence Metadata Matches Online*\n\n"
            f"🎵 *Detected:* *{detected_album}*\n"
            f"👤 *Artist:* *{detected_artist}*\n"
            f"📁 *Files:* {total_audio} audio tracks `({format_tag})`\n\n"
            f"_Audio files have been downloaded to staging. You can apply probed tags, or skip tagging and move directly to your library:_"
        )
        markup = build_metadata_empty_keyboard(session_id)

    return text, markup


async def execute_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    media_url: str,
    genius_input: str = "",
    custom_title: Optional[str] = None,
    quality: Optional[str] = None,
):
    """Executes the download staging phase, queries metadata backends, and prompts user for review."""
    chat = update.effective_chat
    if not chat:
        return

    _clean_expired_sessions()

    user = update.effective_user
    user_id = user.id if user else None
    effective_quality = quality or get_quality_preference(user_id)

    status_msg = await chat.send_message(
        f"⏳ `[1/4]` *Streaming & Extracting Audio ({effective_quality.upper()})...*",
        parse_mode="Markdown",
    )

    loop = asyncio.get_running_loop()

    def sync_status_updater(text: str):
        asyncio.run_coroutine_threadsafe(
            status_msg.edit_text(text, parse_mode="Markdown"), loop
        )

    try:
        stage = await loop.run_in_executor(
            executor,
            download_media_staging,
            media_url,
            genius_input,
            sync_status_updater,
            effective_quality,
        )

        session_id = stage["session_id"]
        detected_album = stage["detected_album"]
        detected_artist = stage["detected_artist"]

        await status_msg.edit_text(
            f"🔎 `[2/4]` *Querying metadata providers for:* `{detected_album}`...\n"
            f"_(Checking iTunes, MusicBrainz, Deezer, Spotify, Discogs)_",
            parse_mode="Markdown",
        )

        # Query metadata providers asynchronously
        candidates = await search_album_metadata_candidates_async(
            album_name=detected_album,
            artist_name=detected_artist,
            local_track_count=len(stage["audio_files"]),
            local_durations=stage["durations"],
        )

        # Fallback to single track query if single track and no album candidate
        if not candidates and stage["is_single"]:
            candidates = await search_track_metadata_candidates_async(
                title=detected_album,
                artist=detected_artist,
                local_duration=stage["durations"][0] if stage["durations"] else None,
            )

        # Cache session state
        _DOWNLOAD_SESSIONS[session_id] = {
            "session_id": session_id,
            "user_id": user_id,
            "chat_id": chat.id,
            "message_id": status_msg.message_id,
            "staging_dir": stage["staging_dir"],
            "detected_artist": detected_artist,
            "detected_album": detected_album,
            "audio_files": stage["audio_files"],
            "durations": stage["durations"],
            "genius_raw": genius_input,
            "parsed_genius": stage.get("parsed_genius"),
            "quality": effective_quality,
            "custom_title": custom_title,
            "candidates": candidates,
            "created_at": time.time(),
        }

        review_text, review_markup = _render_metadata_review_message(
            _DOWNLOAD_SESSIONS[session_id]
        )
        await status_msg.edit_text(
            review_text, reply_markup=review_markup, parse_mode="Markdown"
        )

    except Exception as e:
        logger.exception("Download stage failed")
        await status_msg.edit_text(f"❌ *Download Error:*\n`{e}`", parse_mode="Markdown")


async def download_metadata_callback_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Handles candidate selection, tag diff preview, recommendation approval, and skipping."""
    query = update.callback_query
    if not query or not query.from_user:
        return

    data = query.data or ""
    if not data.startswith("dlmeta_"):
        return

    await query.answer()

    parts = data.split(":")
    action = parts[0]
    session_id = parts[1] if len(parts) > 1 else ""

    session = _DOWNLOAD_SESSIONS.get(session_id)
    if not session or not session["staging_dir"].exists():
        await query.edit_message_text(
            "⚠️ *This download review session has expired or was already processed.*\n"
            "Use `/retag` or `/metadata` to update tags for your library.",
            parse_mode="Markdown",
        )
        return

    candidates: List[MetadataCandidate] = session.get("candidates", [])

    # 1. Quick Action: Accept recommendation
    if action == "dlmeta_rec":
        rec_cand = next((c for c in candidates if c.is_recommended), None)
        if not rec_cand and candidates:
            rec_cand = candidates[0]
        await _execute_finalize(query, session, chosen_candidate=rec_cand)
        return

    # 2. Pick candidate source
    if action == "dlmeta_sel":
        cand_idx = int(parts[2]) if len(parts) > 2 else 0
        if cand_idx < len(candidates):
            chosen = candidates[cand_idx]
            await _execute_finalize(query, session, chosen_candidate=chosen)
        else:
            await _execute_finalize(query, session, chosen_candidate=None)
        return

    # 3. View tag diff / preview
    if action == "dlmeta_diff":
        cand_idx = int(parts[2]) if len(parts) > 2 else 0
        if not candidates or cand_idx >= len(candidates):
            await query.answer("No candidate available.", show_alert=True)
            return

        cand = candidates[cand_idx]
        alb = cand.album_data or cand.track_data
        source_name = cand.source
        conf = int(cand.confidence_score)

        alb_title = getattr(alb, "album", None) or getattr(alb, "title", "Unknown")
        alb_artist = getattr(alb, "artist", session["detected_artist"])
        alb_year = getattr(alb, "year", "") or "Unknown"
        alb_genre = getattr(alb, "genre", "") or resolve_fallback_genre(alb_artist, alb_title)

        has_cover = bool(getattr(alb, "cover_url", None) or getattr(alb, "cover_bytes", None))
        has_lyrics = any(
            bool(t.lyrics_synced or t.lyrics_unsynced) for t in getattr(alb, "tracks", [])
        ) or bool(getattr(alb, "lyrics_synced", None) or getattr(alb, "lyrics_unsynced", None))

        diff_text = (
            f"🔍 *Metadata Tag Diff Preview* `({cand_idx + 1}/{len(candidates)})`\n"
            f"🌐 *Source:* *{source_name}* • 📊 *Confidence:* `{conf}%`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📁 *Staged Raw Media:*\n"
            f"• *Detected Title:* `{session['detected_album']}`\n"
            f"• *Detected Artist:* `{session['detected_artist']}`\n"
            f"• *Total Files:* {len(session['audio_files'])} tracks\n\n"
            f"🏷️ *Candidate Metadata to Apply:*\n"
            f"• *Album:* *{alb_title}*\n"
            f"• *Artist:* *{alb_artist}*\n"
            f"• *Year:* `{alb_year}`\n"
            f"• *Genre:* `{alb_genre}`\n"
            f"• *Cover Art:* {'✅ Hi-Res Jacket available' if has_cover else '❌ Not found'}\n"
            f"• *Lyrics:* {'✅ Companion lyrics available' if has_lyrics else '❌ Standard fetch'}\n"
        )

        tracks = getattr(alb, "tracks", [])
        if tracks:
            diff_text += "\n💿 *Tracklist Preview:*\n"
            for t in tracks[:4]:
                dur = f"({int(t.duration_seconds // 60)}:{int(t.duration_seconds % 60):02d})" if t.duration_seconds > 0 else ""
                diff_text += f"`{t.track_number:02d}.` {t.title[:26]} `{dur}`\n"
            if len(tracks) > 4:
                diff_text += f"_... and {len(tracks) - 4} more tracks_\n"

        markup = build_metadata_diff_keyboard(session_id, cand_idx, len(candidates))
        await query.edit_message_text(diff_text, reply_markup=markup, parse_mode="Markdown")
        return

    # 4. Return to review card
    if action == "dlmeta_back":
        review_text, review_markup = _render_metadata_review_message(session)
        await query.edit_message_text(
            review_text, reply_markup=review_markup, parse_mode="Markdown"
        )
        return

    # 5. Default probed tags
    if action == "dlmeta_default":
        await _execute_finalize(query, session, chosen_candidate=None, skip_tagging=False)
        return

    # 6. Skip tagging & move directly
    if action == "dlmeta_skip":
        await _execute_finalize(query, session, chosen_candidate=None, skip_tagging=True)
        return

    # 7. Discard download
    if action == "dlmeta_cancel":
        staging_dir = session["staging_dir"]
        if staging_dir.exists():
            shutil.rmtree(str(staging_dir), ignore_errors=True)
        _DOWNLOAD_SESSIONS.pop(session_id, None)
        await query.edit_message_text(
            "❌ *Download discarded. Temporary files have been deleted.*",
            parse_mode="Markdown",
        )
        return


async def _execute_finalize(
    query,
    session: Dict[str, Any],
    chosen_candidate: Optional[MetadataCandidate] = None,
    skip_tagging: bool = False,
):
    """Applies metadata & lyrics to staged files, moves them to library, and delivers summary."""
    session_id = session["session_id"]
    source_name = (
        chosen_candidate.source
        if chosen_candidate
        else ("Probed Tags" if not skip_tagging else "Skipped (Raw)")
    )

    action_text = (
        f"⏳ `[3/4]` *Applying metadata from {source_name} & syncing lyrics...*"
        if not skip_tagging
        else "⏳ `[3/4]` *Moving audio files into active library directory...*"
    )
    await query.edit_message_text(action_text, parse_mode="Markdown")

    loop = asyncio.get_running_loop()

    def sync_fin_updater(text: str):
        asyncio.run_coroutine_threadsafe(
            query.edit_message_text(text, parse_mode="Markdown"), loop
        )

    chosen_meta = None
    if chosen_candidate:
        chosen_meta = chosen_candidate.album_data or chosen_candidate.track_data

    try:
        final_dir, meta = await loop.run_in_executor(
            executor,
            finalize_staged_media,
            session["staging_dir"],
            session["detected_artist"],
            session["detected_album"],
            chosen_meta,
            skip_tagging,
            session.get("genius_raw", ""),
            session.get("parsed_genius"),
            sync_fin_updater,
        )

        folder_path = Path(final_dir)
        total_audio = len([
            f
            for f in folder_path.iterdir()
            if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS
        ])
        lrc_count = len([
            f
            for f in folder_path.iterdir()
            if f.is_file() and f.suffix.lower() == ".lrc"
        ])

        flac_cnt = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".flac"])
        opus_cnt = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".opus"])
        m4a_cnt = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".m4a"])
        mp3_cnt = len([f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".mp3"])

        if flac_cnt > 0:
            format_tag = f"{flac_cnt} FLAC (Lossless)"
        elif opus_cnt > 0:
            format_tag = f"{opus_cnt} Opus (Native)"
        elif m4a_cnt > 0:
            format_tag = f"{m4a_cnt} M4A (AAC)"
        else:
            format_tag = f"{mp3_cnt} MP3"

        album_title = meta.get("album", session["detected_album"])
        artist = meta.get("artist", session["detected_artist"])
        year = f" ({meta.get('year')})" if meta.get("year") else ""
        genre = resolve_fallback_genre(artist, album_title, meta.get("genre"))

        custom_t = session.get("custom_title")
        track_summary = f"{custom_t}\n" if custom_t else ""

        caption = (
            f"💿 *{album_title}*{year}\n"
            f"👤 *{artist}*\n"
            f"🏷️ `{genre}`\n"
            f"🌐 *Source:* `{source_name}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{track_summary}"
            f"✓ *Tracks:* {total_audio} files ({format_tag})\n"
            f"✓ *Synced Lyrics:* {lrc_count} `.lrc` files active\n"
            f"📂 *Location:* `{folder_path.name}`\n\n"
            f"✨ *Ready in Symfonium & Navidrome!*"
        )

        if navidrome_client.is_configured():
            scan_res = navidrome_client.start_scan()
            if scan_res.get("ok"):
                caption += "\n🔄 _Navidrome rescan initiated automatically._"

        cover_bytes = meta.get("cover_bytes")
        if not cover_bytes and chosen_meta and getattr(chosen_meta, "cover_bytes", None):
            cover_bytes = chosen_meta.cover_bytes

        if not cover_bytes:
            loose_cov = folder_path / "cover.jpg"
            if loose_cov.is_file():
                try:
                    cover_bytes = loose_cov.read_bytes()
                except Exception:
                    pass

        if cover_bytes:
            try:
                await query.delete_message()
                await query.message.chat.send_photo(
                    photo=io.BytesIO(cover_bytes),
                    caption=caption,
                    parse_mode="Markdown",
                )
                _DOWNLOAD_SESSIONS.pop(session_id, None)
                return
            except Exception as pe:
                logger.warning(f"Could not send photo card: {pe}")

        await query.edit_message_text(caption, parse_mode="Markdown")
        _DOWNLOAD_SESSIONS.pop(session_id, None)

    except Exception as e:
        logger.exception("Finalizing download failed")
        await query.edit_message_text(
            f"❌ *Error applying metadata:*\n`{e}`", parse_mode="Markdown"
        )


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
        await update.message.reply_text(
            "Please provide a search query. Example:\n`/search Amr Diab`"
        )
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
            f"🎯 *Search Results for:* `{query}`\n*Tap a track to download & review:*",
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
    await query.delete_message()
    await execute_task(
        update,
        context,
        chosen_url,
        "",
        custom_title=chosen_title,
        quality=chosen_qual,
    )


# Router export
router = [
    CommandHandler("download", download_handler),
    CommandHandler("genius", genius_handler),
    CommandHandler("search", search_handler),
    CallbackQueryHandler(search_callback_handler, pattern=r"^yt_dl:"),
    CallbackQueryHandler(download_metadata_callback_handler, pattern=r"^dlmeta_"),
    MessageHandler(
        filters.TEXT
        & ~filters.COMMAND
        & filters.Regex(
            r"https?://(?:[\w-]+\.)?(?:spotify\.com|spotify\.link|youtube\.com|youtu\.be|tidal\.com|deezer\.com|qobuz\.com)/\S+"
        ),
        auto_link_handler,
    ),
]
