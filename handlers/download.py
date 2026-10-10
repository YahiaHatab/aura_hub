"""Download handlers: auto-catcher for media links, /download, /genius, /search,
and post-download interactive metadata source review.
"""

import asyncio
import html
import io
import logging
from pathlib import Path
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

import mutagen
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
    MetadataRegistry,
    search_album_metadata_candidates_async,
    search_track_metadata_candidates_async,
)
from services.navidrome import navidrome_client
from services.settings import get_quality_preference, parse_quality_flag
from utils.helpers import resolve_fallback_genre
from utils.keyboards import (
    build_ingest_card_keyboard,
    build_ingest_fallback_keyboard,
    build_ingest_sources_keyboard,
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


def inspect_staging_audio_files(staging_dir: Path) -> Dict[str, Any]:
    """Inspects staging directory audio files using mutagen to extract preliminary artist, album, and track title."""
    audio_files = sorted([
        f for f in staging_dir.iterdir()
        if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS
    ])
    artist = ""
    album = ""
    title = ""
    durations: List[float] = []

    for f in audio_files:
        try:
            mut = mutagen.File(str(f))
            if not mut:
                continue
            if getattr(mut, "info", None) and hasattr(mut.info, "length"):
                durations.append(float(mut.info.length))
            tags = getattr(mut, "tags", None)
            if tags:
                cand_artist = (
                    str(tags.get("artist", [""])[0] if isinstance(tags.get("artist"), list) else tags.get("artist", ""))
                    or str(tags.get("TPE1", [""])[0] if isinstance(tags.get("TPE1"), list) else tags.get("TPE1", ""))
                    or ""
                )
                cand_album = (
                    str(tags.get("album", [""])[0] if isinstance(tags.get("album"), list) else tags.get("album", ""))
                    or str(tags.get("TALB", [""])[0] if isinstance(tags.get("TALB"), list) else tags.get("TALB", ""))
                    or ""
                )
                cand_title = (
                    str(tags.get("title", [""])[0] if isinstance(tags.get("title"), list) else tags.get("title", ""))
                    or str(tags.get("TIT2", [""])[0] if isinstance(tags.get("TIT2"), list) else tags.get("TIT2", ""))
                    or ""
                )
                if cand_artist and not artist:
                    artist = cand_artist
                if cand_album and not album:
                    album = cand_album
                if cand_title and not title:
                    title = cand_title
        except Exception:
            pass

    return {
        "audio_files": audio_files,
        "track_count": len(audio_files),
        "durations": durations,
        "artist": artist,
        "album": album,
        "title": title,
    }


def _render_ingest_card_message(
    session: Dict[str, Any],
    cand_idx: int = 0,
) -> Tuple[str, InlineKeyboardMarkup]:
    """Renders the HTML staging ingestion card and inline keyboard."""
    session_id: str = session["session_id"]
    detected_album: str = session.get("detected_album", "Unknown Album")
    detected_artist: str = session.get("detected_artist", "Unknown Artist")
    staging_rel_path: str = session.get("staging_rel_path", "")
    track_count: int = session.get("track_count", len(session.get("audio_files", [])))
    candidates: List[MetadataCandidate] = session.get("candidates", [])

    esc_album = html.escape(detected_album)
    esc_artist = html.escape(detected_artist)
    esc_staging = html.escape(staging_rel_path)

    if not candidates:
        text = (
            f"🎵 <b>Download Complete: Ingestion Staging</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Release:</b> {esc_album} — {esc_artist}\n"
            f"<b>Files:</b> {track_count} tracks staging in <code>{esc_staging}</code>\n\n"
            f"⚠️ <b>No online metadata found.</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Select an action below or fine-tune in Web Studio:"
        )
        markup = build_ingest_fallback_keyboard(session_id, staging_rel_path)
        return text, markup

    cand_idx = max(0, min(cand_idx, len(candidates) - 1))
    top_cand = candidates[cand_idx]

    p = top_cand.preview or {}
    has_cover = bool(
        p.get("has_cover")
        or (top_cand.album_data and (top_cand.album_data.cover_url or top_cand.album_data.cover_bytes))
        or (top_cand.track_data and (top_cand.track_data.cover_url or top_cand.track_data.cover_bytes))
    )
    has_lyrics = bool(
        p.get("has_lyrics")
        or (top_cand.track_data and (top_cand.track_data.lyrics_synced or top_cand.track_data.lyrics_unsynced))
        or (top_cand.album_data and any(t.lyrics_synced or t.lyrics_unsynced for t in getattr(top_cand.album_data, "tracks", [])))
    )

    cover_art_status = "Cover Art" if has_cover else "No Cover"
    lyrics_status = "Synced Lyrics" if has_lyrics else "No Lyrics"
    confidence_score = int(top_cand.confidence_score)

    badge_label = html.escape(top_cand.badge_label or top_cand.source)
    source_name = html.escape((top_cand.source or "ONLINE").upper())

    text = (
        f"🎵 <b>Download Complete: Ingestion Staging</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Release:</b> {esc_album} — {esc_artist}\n"
        f"<b>Files:</b> {track_count} tracks staging in <code>{esc_staging}</code>\n\n"
        f"⭐️ <b>Top Recommendation:</b>\n"
        f"• <b>Source:</b> {badge_label} ({source_name})\n"
        f"• <b>Confidence / Match:</b> {confidence_score}%\n"
        f"• <b>Includes:</b> {cover_art_status} | {lyrics_status} | High-Res Tags\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Select an action below or fine-tune in Web Studio:"
    )
    markup = build_ingest_card_keyboard(
        session_id=session_id,
        staging_rel_path=staging_rel_path,
        cand_idx=cand_idx,
        alt_count=len(candidates),
    )
    return text, markup


def _render_ingest_sources_message(
    session: Dict[str, Any],
) -> Tuple[str, InlineKeyboardMarkup]:
    """Renders the HTML alternative sources selection menu."""
    session_id: str = session["session_id"]
    detected_album: str = session.get("detected_album", "Unknown Album")
    detected_artist: str = session.get("detected_artist", "Unknown Artist")
    candidates: List[MetadataCandidate] = session.get("candidates", [])
    active_idx: int = session.get("active_cand_idx", 0)

    esc_album = html.escape(detected_album)
    esc_artist = html.escape(detected_artist)

    cand_lines = []
    for idx, c in enumerate(candidates, 1):
        badge = html.escape(c.badge_label or c.source)
        source = html.escape((c.source or "").upper())
        p = c.preview or {}
        title = html.escape(p.get("title") or c.source)
        year = html.escape(str(p.get("year", "")))
        year_str = f" ({year})" if year else ""
        conf = int(c.confidence_score)
        mark = "👉 " if (idx - 1) == active_idx else "• "
        cand_lines.append(f"{mark}<b>[{badge}]</b> {title}{year_str} — <i>{source}</i> ({conf}% match)")

    text = (
        f"📋 <b>Alternative Metadata Sources</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Release:</b> {esc_album} — {esc_artist}\n"
        f"<b>Available Providers:</b> {len(candidates)} candidates found\n\n"
        + "\n".join(cand_lines) + "\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Select a source below to preview and apply, or return back:"
    )
    markup = build_ingest_sources_keyboard(session_id, candidates, active_idx=active_idx)
    return text, markup


async def execute_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    media_url: str,
    genius_input: str = "",
    custom_title: Optional[str] = None,
    quality: Optional[str] = None,
):
    """Executes the download staging phase, queries metadata providers, and prompts user with interactive card."""
    chat = update.effective_chat
    if not chat:
        return

    _clean_expired_sessions()

    user = update.effective_user
    user_id = user.id if user else None
    effective_quality = quality or get_quality_preference(user_id)

    status_msg = await chat.send_message(
        f"⏳ <code>[1/4]</code> <b>Streaming & Extracting Audio ({html.escape(effective_quality.upper())})...</b>",
        parse_mode="HTML",
    )

    loop = asyncio.get_running_loop()

    def sync_status_updater(text: str):
        clean_text = text.replace("*", "").replace("`", "")
        asyncio.run_coroutine_threadsafe(
            status_msg.edit_text(f"⏳ <code>[1/4]</code> <b>{html.escape(clean_text)}</b>", parse_mode="HTML"), loop
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
        staging_dir = stage["staging_dir"]

        # Inspect downloaded audio files in staging folder using mutagen
        stg_info = inspect_staging_audio_files(staging_dir)
        detected_album = stg_info["album"] or stage["detected_album"]
        detected_artist = stg_info["artist"] or stage["detected_artist"]
        staging_rel_path = str(staging_dir.relative_to(config.BASE_DOWNLOAD_DIR)).replace("\\", "/")

        await status_msg.edit_text(
            f"🔎 <code>[2/4]</code> <b>Querying metadata providers for:</b> <code>{html.escape(detected_album)}</code>...\n"
            f"<i>(Checking Spotify, Deezer, MusicBrainz, iTunes, Discogs)</i>",
            parse_mode="HTML",
        )

        # Call MetadataRegistry.aggregate_search concurrently across registered providers
        try:
            candidates = await MetadataRegistry.aggregate_search(
                artist=detected_artist,
                album=detected_album,
                local_track_count=stg_info["track_count"],
                local_durations=stg_info["durations"],
                is_single=stage["is_single"],
            )
        except Exception as me:
            logger.warning(f"Metadata aggregate search failed: {me}")
            candidates = []

        # Cache session state
        session_data = {
            "session_id": session_id,
            "user_id": user_id,
            "chat_id": chat.id,
            "message_id": status_msg.message_id,
            "staging_dir": staging_dir,
            "staging_rel_path": staging_rel_path,
            "detected_artist": detected_artist,
            "detected_album": detected_album,
            "audio_files": stg_info["audio_files"],
            "durations": stg_info["durations"],
            "track_count": stg_info["track_count"],
            "genius_raw": genius_input,
            "parsed_genius": stage.get("parsed_genius"),
            "quality": effective_quality,
            "custom_title": custom_title,
            "candidates": candidates,
            "active_cand_idx": 0,
            "created_at": time.time(),
        }
        _DOWNLOAD_SESSIONS[session_id] = session_data
        if context and hasattr(context, "bot_data"):
            context.bot_data.setdefault("ingest_sessions", {})[session_id] = session_data

        review_text, review_markup = _render_ingest_card_message(session_data, cand_idx=0)
        await status_msg.edit_text(
            review_text, reply_markup=review_markup, parse_mode="HTML"
        )

    except Exception as e:
        logger.exception("Download stage failed")
        await status_msg.edit_text(
            f"❌ <b>Download Error:</b>\n<code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )


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


async def download_ingest_callback_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Handles 1-tap ingestion commits, source selection submenu, skip, and back actions."""
    query = update.callback_query
    if not query or not query.from_user:
        return

    data = query.data or ""
    if not data.startswith("ingest:"):
        return

    # Verify admin authorization
    user_id = query.from_user.id
    if user_id not in config.ADMIN_USER_IDS:
        await query.answer("⛔ Unauthorized: Admin privileges required.", show_alert=True)
        return

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    session_id = parts[2] if len(parts) > 2 else ""

    session = (
        (context.bot_data.get("ingest_sessions", {}).get(session_id) if context and hasattr(context, "bot_data") else None)
        or _DOWNLOAD_SESSIONS.get(session_id)
    )

    if not session or not session.get("staging_dir") or not session["staging_dir"].exists():
        await query.answer("⚠️ Session expired or files already imported.", show_alert=True)
        await query.edit_message_text(
            "⚠️ <b>This ingestion session has expired or was already processed.</b>\n"
            "Use <code>/retag</code> or Web Studio to inspect your library.",
            parse_mode="HTML",
        )
        return

    candidates: List[MetadataCandidate] = session.get("candidates", [])

    # 1. ingest:apply:<session_id>:<cand_idx>
    if action == "apply":
        await query.answer("Committing metadata & importing...")
        cand_idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else session.get("active_cand_idx", 0)
        chosen_cand = candidates[cand_idx] if candidates and cand_idx < len(candidates) else (candidates[0] if candidates else None)
        await _execute_ingest_finalize(query, session, chosen_candidate=chosen_cand, skip_tagging=False)
        return

    # 2. ingest:skip:<session_id>
    if action == "skip":
        await query.answer("Importing without tag changes...")
        await _execute_ingest_finalize(query, session, chosen_candidate=None, skip_tagging=True)
        return

    # 3. ingest:sources:<session_id>
    if action == "sources":
        await query.answer()
        if not candidates:
            await query.answer("No alternative metadata candidates found.", show_alert=True)
            return
        text, markup = _render_ingest_sources_message(session)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        return

    # 4. ingest:select:<session_id>:<cand_idx>
    if action == "select":
        cand_idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        session["active_cand_idx"] = cand_idx
        chosen = candidates[cand_idx] if cand_idx < len(candidates) else candidates[0]
        badge = chosen.badge_label or chosen.source
        await query.answer(f"Switched to {badge}!")
        text, markup = _render_ingest_card_message(session, cand_idx=cand_idx)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        return

    # 5. ingest:back:<session_id>
    if action == "back":
        await query.answer()
        active_idx = session.get("active_cand_idx", 0)
        text, markup = _render_ingest_card_message(session, cand_idx=active_idx)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        return


async def _execute_ingest_finalize(
    query,
    session: Dict[str, Any],
    chosen_candidate: Optional[MetadataCandidate] = None,
    skip_tagging: bool = False,
):
    """Applies candidate tags, embeds cover art & lyrics, moves files to library, and triggers Navidrome scan."""
    session_id = session["session_id"]
    source_name = (
        chosen_candidate.source
        if chosen_candidate
        else ("Probed Tags" if not skip_tagging else "Skipped (Raw)")
    )

    action_text = (
        f"⏳ <code>[3/4]</code> <b>Applying metadata from {html.escape(source_name)} & syncing lyrics...</b>"
        if not skip_tagging
        else "⏳ <code>[3/4]</code> <b>Moving audio files into active library directory...</b>"
    )
    await query.edit_message_text(action_text, parse_mode="HTML")

    loop = asyncio.get_running_loop()

    def sync_fin_updater(text: str):
        clean_msg = text.replace("*", "").replace("`", "")
        asyncio.run_coroutine_threadsafe(
            query.edit_message_text(f"⏳ <code>[3/4]</code> <b>{html.escape(clean_msg)}</b>", parse_mode="HTML"), loop
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

        album_title = html.escape(meta.get("album", session["detected_album"]))
        artist = html.escape(meta.get("artist", session["detected_artist"]))
        year = f" ({meta.get('year')})" if meta.get("year") else ""
        genre = html.escape(resolve_fallback_genre(meta.get("artist", session["detected_artist"]), meta.get("album", session["detected_album"]), meta.get("genre")))

        # Subsonic Library Rescan
        rescan_note = ""
        if navidrome_client.is_configured():
            scan_res = navidrome_client.start_scan()
            if scan_res.get("ok"):
                rescan_note = "\n🔄 <i>Navidrome library rescan initiated automatically.</i>"

        if skip_tagging:
            caption = (
                f"⏭ <b>Imported without tag changes.</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Release:</b> {album_title} — {artist}\n"
                f"<b>Files:</b> {total_audio} files ({format_tag})\n"
                f"<b>Location:</b> <code>{html.escape(folder_path.name)}</code>\n"
                f"{rescan_note}\n\n"
                f"✨ <i>Ready in Navidrome!</i>"
            )
        else:
            caption = (
                f"✅ <b>Successfully tagged and imported to Navidrome Library!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"💿 <b>{album_title}</b>{year}\n"
                f"👤 <b>{artist}</b>\n"
                f"🏷️ <code>{genre}</code>\n"
                f"🌐 <b>Source:</b> <code>{html.escape(source_name)}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"✓ <b>Tracks:</b> {total_audio} files ({format_tag})\n"
                f"✓ <b>Synced Lyrics:</b> {lrc_count} <code>.lrc</code> files active\n"
                f"📂 <b>Location:</b> <code>{html.escape(folder_path.name)}</code>\n"
                f"{rescan_note}\n\n"
                f"✨ <b>Ready in Symfonium & Navidrome!</b>"
            )

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

        # Cleanup sessions
        _DOWNLOAD_SESSIONS.pop(session_id, None)

        if cover_bytes:
            try:
                await query.delete_message()
                await query.message.chat.send_photo(
                    photo=io.BytesIO(cover_bytes),
                    caption=caption,
                    parse_mode="HTML",
                )
                return
            except Exception as pe:
                logger.warning(f"Could not send photo card: {pe}")

        await query.edit_message_text(caption, parse_mode="HTML")

    except Exception as e:
        logger.exception("Finalizing download failed")
        await query.edit_message_text(
            f"❌ <b>Error applying metadata:</b>\n<code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
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
    CallbackQueryHandler(download_ingest_callback_handler, pattern=r"^ingest:"),
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
