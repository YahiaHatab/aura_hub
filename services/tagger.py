"""Mutagen ID3 & Vorbis tagging engine for Aura Hub.

Implements duration/phonetic track alignment, loose cover.jpg generation for Navidrome,
and comprehensive ID3v2.3 (MP3) and Vorbis comment (Opus) tagging.
"""

import base64
import logging
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from mutagen.flac import Picture
from mutagen.id3 import (
    APIC,
    ID3,
    IPLS,
    TALB,
    TCOM,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TXXX,
    USLT,
    ID3NoHeaderError,
)
from mutagen.mp3 import MP3
from mutagen.oggopus import OggOpus

from services.metadata import (
    get_genius_client,
    search_genius_album,
    search_musicbrainz_release,
    search_musicbrainz_track,
)
from utils.helpers import extract_clean_artists, franco_to_arabic, get_clean_name

logger = logging.getLogger(__name__)

SUPPORTED_AUDIO_EXTENSIONS = {".mp3", ".opus"}


def find_best_track_match(
    local_file_path: Union[str, Path],
    local_title: str,
    file_seq: int,
    mb_tracks: List[Dict[str, Any]],
    assigned_positions: Set[int],
) -> Tuple[str, int]:
    """Matches local MP3 or Opus against MusicBrainz tracks using Text Similarity + Audio Duration tolerance.

    Avoids index corruption when YouTube playlists are shuffled, reversed, or misnumbered.
    """
    if not mb_tracks:
        return local_title, file_seq

    # 1. Obtain local audio duration via mutagen
    local_duration = 0.0
    try:
        path_obj = Path(local_file_path)
        ext = path_obj.suffix.lower()
        if ext == ".mp3":
            audio_info = MP3(str(path_obj))
            local_duration = float(audio_info.info.length)
        elif ext == ".opus":
            audio_info = OggOpus(str(path_obj))
            local_duration = float(audio_info.info.length)
        else:
            import mutagen
            audio_info = mutagen.File(str(path_obj))
            if audio_info and audio_info.info:
                local_duration = float(audio_info.info.length)
    except Exception as e:
        logger.debug(f"Could not read duration for {local_file_path}: {e}")

    title_variants = franco_to_arabic(local_title)
    best_candidate: Optional[Dict[str, Any]] = None
    best_score = -1

    for trk in mb_tracks:
        pos = trk.get("position")
        if pos in assigned_positions:
            continue

        mb_title = trk.get("title", "")
        mb_length = trk.get("length", 0.0)
        score = 0
        clean_mb = get_clean_name(mb_title)

        # Word/Sub-token phonetic overlap
        for v in title_variants:
            v_clean = get_clean_name(v)
            v_words = [w for w in v_clean.split() if len(w) > 2]
            matched_words = sum(1 for w in v_words if w in clean_mb)
            if matched_words > 0:
                score += (matched_words * 40)

        # Audio duration comparison (±4s tolerance gives strong bonus)
        if local_duration > 0 and mb_length > 0:
            diff = abs(local_duration - mb_length)
            if diff <= 2.5:
                score += 80
            elif diff <= 5.0:
                score += 45
            elif diff <= 9.0:
                score += 20
            else:
                score -= 25

        if score > best_score:
            best_score = score
            best_candidate = trk

    # Accept candidate if it achieved a solid confidence threshold
    if best_candidate and best_score >= 35:
        assigned_positions.add(best_candidate.get("position"))
        return str(best_candidate.get("title")), int(best_candidate.get("position", file_seq))

    # 2. Sequential fallback if position index has not been claimed
    for trk in mb_tracks:
        pos = trk.get("position")
        if pos == file_seq and file_seq not in assigned_positions:
            assigned_positions.add(file_seq)
            return str(trk.get("title")), file_seq

    # 3. Last fallback: first unassigned position
    for trk in mb_tracks:
        pos = trk.get("position")
        if pos not in assigned_positions:
            assigned_positions.add(pos)
            return str(trk.get("title")), int(pos)

    return local_title, file_seq


def tag_album_hybrid(
    folder: Union[str, Path],
    album_name: str,
    artist_name: str,
    genius_raw: str = "",
    parsed_genius: Optional[Dict[str, Any]] = None,
    status_updater: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Applies multi-stage metadata tagging across all MP3 and Opus files in an album folder.

    Stages:
    1. MusicBrainz & AcoustID resolution (with Latin-Canonical artist normalization)
    2. Genius credits (composer, producer, lyrics)
    3. Cover Art Archive high-res retrieval and loose cover.jpg writing
    4. Mutagen ID3 (MP3) & Vorbis Comment (Opus) embedding with duration alignment
    """
    folder_path = Path(folder)
    files = sorted([f for f in folder_path.iterdir() if f.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS])
    sample_track = str(files[0]) if files else None

    if status_updater:
        status_updater("⚡ `[2/4]` *Querying MusicBrainz & Acoustic Fingerprints...*")

    mb_data = search_musicbrainz_release(
        album_name, artist_name, sample_file=sample_track
    )

    if mb_data and mb_data.get("artist"):
        effective_artist = mb_data["artist"]
    else:
        latin_cands = [a for a in extract_clean_artists(artist_name) if a.isascii()]
        effective_artist = latin_cands[0] if latin_cands else artist_name

    resolved_album = (mb_data.get("title") if mb_data else None) or album_name

    genius_album = search_genius_album(
        resolved_album, effective_artist, parsed_genius=parsed_genius, genius_raw=genius_raw
    )
    genius = get_genius_client()

    if status_updater:
        status_updater("🎨 `[3/4]` *Acquiring Cover Art Archive Jacket...*")

    cover_bytes = mb_data.get("cover_bytes") if mb_data else None
    if not cover_bytes and genius_album:
        cover_url = getattr(genius_album, "cover_art_url", None)
        if cover_url:
            try:
                req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    cover_bytes = r.read()
            except Exception as e:
                logger.debug(f"Failed to fetch Genius cover art: {e}")

    # Write loose cover.jpg inside the folder for Navidrome directory indexing
    if cover_bytes:
        try:
            cover_file = folder_path / "cover.jpg"
            cover_file.write_bytes(cover_bytes)
            logger.info(f"Saved loose cover.jpg for Navidrome in {folder_path.name}")
        except Exception as e:
            logger.warning(f"Failed to write loose cover.jpg: {e}")

    total_tracks = len(files)
    if mb_data and mb_data.get("tracks"):
        total_tracks = max(len(files), len(mb_data["tracks"]))
    elif genius_album and hasattr(genius_album, "tracks"):
        total_tracks = max(len(files), len(genius_album.tracks))

    assigned_positions: Set[int] = set()
    mb_tracklist = mb_data.get("tracks", []) if mb_data else []

    for idx_file, file_path in enumerate(files, 1):
        num_match = re.match(r"^(\d+)\s*[-.]\s*(.*)", file_path.stem)
        if num_match:
            file_seq = int(num_match.group(1))
            local_title = num_match.group(2).strip()
        else:
            file_seq = idx_file
            local_title = file_path.stem

        # Resolve track using title phonetics + duration comparison
        matched_title, final_track_num = find_best_track_match(
            file_path, local_title, file_seq, mb_tracklist, assigned_positions
        )

        # Match corresponding Genius song for composer & producer credits
        matched_genius_song = None
        if genius_album and hasattr(genius_album, "tracks"):
            clean_matched = get_clean_name(matched_title)
            for g_idx, item in enumerate(genius_album.tracks, 1):
                s_obj = item[1] if isinstance(item, tuple) else getattr(item, "song", item)
                clean_g = get_clean_name(getattr(s_obj, "title", ""))
                if any(w in clean_g for w in clean_matched.split() if len(w) > 2) or g_idx == final_track_num:
                    matched_genius_song = s_obj
                    break

        song_dict: Dict[str, Any] = {}
        lyrics_text = None
        if matched_genius_song and genius:
            sid = getattr(matched_genius_song, "id_", None) or (
                matched_genius_song._body.get("id")
                if hasattr(matched_genius_song, "_body")
                else None
            )
            lyrics_text = getattr(matched_genius_song, "lyrics", None)
            if sid:
                try:
                    song_dict = genius.song(sid).get("song", {})
                    if not lyrics_text:
                        lyrics_text = genius.lyrics(sid)
                except Exception:
                    song_dict = getattr(matched_genius_song, "_body", {})

        prods = [p["name"] for p in song_dict.get("producer_artists", []) if "name" in p]
        writers = [w["name"] for w in song_dict.get("writer_artists", []) if "name" in w]

        f_ext = file_path.suffix.lower()

        # Tag MP3 files with ID3v2.3
        if f_ext == ".mp3":
            try:
                audio = ID3(str(file_path))
            except ID3NoHeaderError:
                audio = ID3()

            audio.delall("TIT2")
            audio.add(TIT2(encoding=3, text=matched_title))

            if effective_artist and effective_artist.lower() not in ("unknown artist", "various"):
                audio.delall("TPE1")
                audio.add(TPE1(encoding=3, text=effective_artist))
                audio.delall("TPE2")
                audio.add(TPE2(encoding=3, text=effective_artist))

            audio.delall("TALB")
            audio.add(TALB(encoding=3, text=resolved_album))

            audio.delall("TRCK")
            audio.add(TRCK(encoding=3, text=f"{final_track_num}/{total_tracks}"))
            audio.delall("TPOS")
            audio.add(TPOS(encoding=3, text="1/1"))

            if mb_data and mb_data.get("date"):
                audio.delall("TDRC")
                audio.add(TDRC(encoding=3, text=str(mb_data["date"])))

            if mb_data and mb_data.get("genre"):
                audio.delall("TCON")
                audio.add(TCON(encoding=3, text=mb_data["genre"]))

            if lyrics_text:
                audio.delall("USLT")
                audio.add(USLT(encoding=3, lang="ara", desc="", text=lyrics_text))

            if prods:
                p_str = ", ".join(prods)
                audio.add(TXXX(encoding=3, desc="PRODUCER", text=p_str))
                audio.add(IPLS(encoding=3, people=[("producer", p_str)]))

            if writers:
                audio.delall("TCOM")
                audio.add(TCOM(encoding=3, text=", ".join(writers)))

            if cover_bytes:
                audio.delall("APIC")
                audio.add(
                    APIC(
                        encoding=3,
                        mime="image/jpeg",
                        type=3,
                        desc="Cover",
                        data=cover_bytes,
                    )
                )

            audio.save(str(file_path), v2_version=3)

        # Tag Opus files with Vorbis comments
        elif f_ext == ".opus":
            try:
                audio = OggOpus(str(file_path))
                audio["title"] = [matched_title]

                if effective_artist and effective_artist.lower() not in ("unknown artist", "various"):
                    audio["artist"] = [effective_artist]
                    audio["albumartist"] = [effective_artist]

                audio["album"] = [resolved_album]
                audio["tracknumber"] = [str(final_track_num)]
                audio["totaltracks"] = [str(total_tracks)]
                audio["discnumber"] = ["1"]
                audio["totaldiscs"] = ["1"]

                if mb_data and mb_data.get("date"):
                    audio["date"] = [str(mb_data["date"])]

                if mb_data and mb_data.get("genre"):
                    audio["genre"] = [mb_data["genre"]]

                if lyrics_text:
                    audio["lyrics"] = [lyrics_text]

                if prods:
                    audio["producer"] = [", ".join(prods)]

                if writers:
                    audio["composer"] = [", ".join(writers)]

                if cover_bytes:
                    p = Picture()
                    p.data = cover_bytes
                    p.type = 3
                    p.mime = "image/jpeg"
                    p.desc = "Cover"
                    audio["metadata_block_picture"] = [base64.b64encode(p.write()).decode("ascii")]

                audio.save()
            except Exception as opus_err:
                logger.warning(f"Failed to tag Opus track {file_path.name}: {opus_err}")

    return {
        "album": resolved_album,
        "artist": effective_artist,
        "year": (mb_data.get("date") if mb_data else None) or "",
        "genre": (mb_data.get("genre") if mb_data else None) or "Music",
        "cover_bytes": cover_bytes,
    }


def tag_playlist_hybrid(
    folder: Union[str, Path],
    status_updater: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Tags individual tracks in a custom playlist folder supporting MP3 and Opus."""
    folder_path = Path(folder)
    genius = get_genius_client()
    files = sorted([f for f in folder_path.iterdir() if f.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS])

    if status_updater:
        status_updater("⚡ `[2/4]` *Tagging Playlist Tracks via MusicBrainz...*")

    for file_path in files:
        stem = file_path.stem
        if " - " in stem:
            parts = stem.split(" - ", 1)
            artist_q, title_q = parts[0].strip(), parts[1].strip()
        else:
            artist_q, title_q = "", stem.strip()

        mb_trk = search_musicbrainz_track(title_q, artist_q)
        resolved_title = mb_trk.get("title") or title_q
        resolved_artist = mb_trk.get("artist") or artist_q
        resolved_genre = mb_trk.get("genre") or ""

        song = None
        if genius:
            try:
                song = genius.search_song(resolved_title, resolved_artist)
            except Exception:
                pass

        lyrics_text = getattr(song, "lyrics", None) if song else None
        if song and genius:
            sid = getattr(song, "id_", None) or (
                song._body.get("id") if hasattr(song, "_body") else None
            )
            if sid and not lyrics_text:
                try:
                    lyrics_text = genius.lyrics(sid)
                except Exception:
                    pass

        f_ext = file_path.suffix.lower()
        if f_ext == ".mp3":
            try:
                audio = ID3(str(file_path))
            except ID3NoHeaderError:
                audio = ID3()

            audio.delall("TIT2")
            audio.add(TIT2(encoding=3, text=resolved_title))
            if resolved_artist:
                audio.delall("TPE1")
                audio.add(TPE1(encoding=3, text=resolved_artist))
                audio.delall("TPE2")
                audio.add(TPE2(encoding=3, text=resolved_artist))

            if resolved_genre:
                audio.delall("TCON")
                audio.add(TCON(encoding=3, text=resolved_genre))

            if lyrics_text:
                audio.delall("USLT")
                audio.add(USLT(encoding=3, lang="eng", desc="", text=lyrics_text))

            audio.save(str(file_path), v2_version=3)

        elif f_ext == ".opus":
            try:
                audio = OggOpus(str(file_path))
                audio["title"] = [resolved_title]
                if resolved_artist:
                    audio["artist"] = [resolved_artist]
                    audio["albumartist"] = [resolved_artist]
                if resolved_genre:
                    audio["genre"] = [resolved_genre]
                if lyrics_text:
                    audio["lyrics"] = [lyrics_text]
                audio.save()
            except Exception as e:
                logger.warning(f"Failed to tag playlist Opus track {file_path.name}: {e}")

    return {
        "album": "Custom Playlist",
        "artist": "Various Artists",
        "year": "",
        "genre": "Mixed",
        "cover_bytes": None,
    }
