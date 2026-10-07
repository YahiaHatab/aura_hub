"""Mutagen ID3 & Vorbis tagging engine for Aura Hub.

Implements:
- Comprehensive ID3v2.3 (MP3) tagging (TIT2, TPE1, TPE2, TALB, TRCK, TPOS, TDRC, TCON, TCOM, TEXT, TXXX/IPLS, USLT, APIC)
- Native FLAC Vorbis comments (TITLE, ARTIST, ALBUMARTIST, ALBUM, TRACKNUMBER, TRACKTOTAL, DISCNUMBER, DISCTOTAL, DATE, GENRE, COMPOSER, PRODUCER, LYRICS) and Picture
- Native Opus Vorbis comments (title, artist, albumartist, album, tracknumber, totaltracks, discnumber, totaldiscs, date, genre, composer, producer, lyrics) and metadata_block_picture
- MP4/M4A tagging atoms (\xa9nam, \xa9ART, aART, \xa9alb, trkn, disk, \xa9day, \xa9gen, \xa9wrt, \xa9lyr, covr)
- Loose cover.jpg generation for Navidrome library indexing
- Synced lyrics (.lrc) integration via LRCLIB
- Acceptance of user-chosen unified metadata payloads
"""

import base64
import logging
import os
from pathlib import Path
import re
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    ID3,
    IPLS,
    TALB,
    TCOM,
    TCON,
    TDRC,
    TEXT,
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
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus

import config
from services.lyrics import fetch_and_save_lrc
from services.metadata import (
    UnifiedAlbumMetadata,
    UnifiedTrackMetadata,
    calculate_best_variant_similarity,
    calculate_string_similarity,
    fetch_image_bytes_async,
    get_genius_client,
    search_genius_album,
    search_musicbrainz_release,
    search_musicbrainz_track,
)
from utils.helpers import (
    extract_clean_artists,
    franco_to_arabic,
    get_clean_name,
    is_arabic_music,
    resolve_fallback_genre,
)

logger = logging.getLogger(__name__)

SUPPORTED_AUDIO_EXTENSIONS = set(config.AUDIO_EXTENSIONS)


# =====================================================================
# LOOSE COVER ART HELPER (NAVIDROME COMPATIBILITY)
# =====================================================================

def write_loose_cover(folder: Union[str, Path], cover_bytes: bytes) -> Optional[Path]:
    """Saves loose cover.jpg inside an album directory for Navidrome indexing."""
    if not cover_bytes:
        return None
    try:
        folder_path = Path(folder)
        cover_path = folder_path / "cover.jpg"
        if not cover_path.exists() or cover_path.stat().st_size == 0:
            cover_path.write_bytes(cover_bytes)
        logger.info(f"Saved loose cover.jpg for Navidrome in {folder_path.name}")
        return cover_path
    except Exception as e:
        logger.warning(f"Failed to write loose cover.jpg: {e}")
        return None


# =====================================================================
# DURATION & PHONETIC TRACK ALIGNMENT
# =====================================================================

def find_best_track_match(
    local_file_path: Union[str, Path],
    local_title: str,
    file_seq: int,
    mb_tracks: List[Dict[str, Any]],
    assigned_positions: Set[int],
) -> Tuple[str, int]:
    """Matches local audio file against metadata tracklist using Text Similarity + Duration tolerance."""
    if not mb_tracks:
        return local_title, file_seq

    local_duration = 0.0
    try:
        audio_info = mutagen.File(str(local_file_path))
        if audio_info and getattr(audio_info, "info", None) and hasattr(audio_info.info, "length"):
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

        # Audio duration comparison (tolerance scoring)
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

    if best_candidate and best_score >= 35:
        assigned_positions.add(best_candidate.get("position"))
        return str(best_candidate.get("title")), int(best_candidate.get("position", file_seq))

    # Sequential fallback
    for trk in mb_tracks:
        pos = trk.get("position")
        if pos == file_seq and file_seq not in assigned_positions:
            assigned_positions.add(file_seq)
            return str(trk.get("title")), file_seq

    # First unassigned position
    for trk in mb_tracks:
        pos = trk.get("position")
        if pos not in assigned_positions:
            assigned_positions.add(pos)
            return str(trk.get("title")), int(pos)

    return local_title, file_seq


# =====================================================================
# UNIFIED FILE TAGGER (MP3, FLAC, OPUS, M4A)
# =====================================================================

def apply_unified_metadata_to_file(
    file_path: Union[str, Path],
    track: UnifiedTrackMetadata,
    cover_bytes: Optional[bytes] = None,
) -> bool:
    """Writes complete unified metadata tags across ID3, Vorbis, and MP4 containers."""
    path = Path(file_path)
    if not path.is_file():
        return False

    effective_cover = cover_bytes or track.cover_bytes
    ext = path.suffix.lower()

    try:
        # 1. MP3 (ID3v2.3)
        if ext == ".mp3":
            try:
                audio = ID3(str(path))
            except ID3NoHeaderError:
                audio = ID3()

            if track.title:
                audio.delall("TIT2")
                audio.add(TIT2(encoding=3, text=track.title))

            if track.artist:
                audio.delall("TPE1")
                audio.add(TPE1(encoding=3, text=track.artist))

            album_art = track.album_artist or track.artist
            if album_art:
                audio.delall("TPE2")
                audio.add(TPE2(encoding=3, text=album_art))

            if track.album:
                audio.delall("TALB")
                audio.add(TALB(encoding=3, text=track.album))

            tot = track.total_tracks or 1
            audio.delall("TRCK")
            audio.add(TRCK(encoding=3, text=f"{track.track_number}/{tot}"))

            tot_discs = track.total_discs or 1
            audio.delall("TPOS")
            audio.add(TPOS(encoding=3, text=f"{track.disc_number}/{tot_discs}"))

            if track.year:
                audio.delall("TDRC")
                audio.add(TDRC(encoding=3, text=str(track.year)))

            genre_val = track.genre or (", ".join(track.genres[:2]) if track.genres else "")
            if genre_val:
                audio.delall("TCON")
                audio.add(TCON(encoding=3, text=genre_val))

            if track.composers:
                audio.delall("TCOM")
                audio.add(TCOM(encoding=3, text=", ".join(track.composers)))

            if track.lyricists:
                audio.delall("TEXT")
                audio.add(TEXT(encoding=3, text=", ".join(track.lyricists)))

            if track.producers:
                p_str = ", ".join(track.producers)
                audio.add(TXXX(encoding=3, desc="PRODUCER", text=p_str))
                audio.add(IPLS(encoding=3, people=[("producer", p_str)]))

            if track.arrangers:
                audio.add(TXXX(encoding=3, desc="ARRANGER", text=", ".join(track.arrangers)))

            lyrics_text = track.lyrics_unsynced or track.lyrics_synced
            if lyrics_text:
                audio.delall("USLT")
                audio.add(USLT(encoding=3, lang="eng", desc="", text=lyrics_text))

            if effective_cover:
                audio.delall("APIC")
                mime = "image/png" if effective_cover.startswith(b"\x89PNG") else "image/jpeg"
                audio.add(
                    APIC(
                        encoding=3,
                        mime=mime,
                        type=3,
                        desc="Cover",
                        data=effective_cover,
                    )
                )

            audio.save(str(path), v2_version=3)
            return True

        # 2. FLAC (Vorbis comments & Picture)
        elif ext == ".flac":
            audio = FLAC(str(path))
            if track.title:
                audio["title"] = [track.title]
            if track.artist:
                audio["artist"] = [track.artist]
            album_art = track.album_artist or track.artist
            if album_art:
                audio["albumartist"] = [album_art]
            if track.album:
                audio["album"] = [track.album]

            audio["tracknumber"] = [str(track.track_number)]
            tot = str(track.total_tracks or 1)
            audio["tracktotal"] = [tot]
            audio["totaltracks"] = [tot]

            disc = str(track.disc_number or 1)
            tot_discs = str(track.total_discs or 1)
            audio["discnumber"] = [disc]
            audio["disctotal"] = [tot_discs]
            audio["totaldiscs"] = [tot_discs]

            if track.year:
                audio["date"] = [str(track.year)]
                audio["year"] = [str(track.year)[:4]]

            genre_val = track.genre or (", ".join(track.genres[:2]) if track.genres else "")
            if genre_val:
                audio["genre"] = [genre_val]

            if track.composers:
                audio["composer"] = [", ".join(track.composers)]
            if track.producers:
                audio["producer"] = [", ".join(track.producers)]
            if track.arrangers:
                audio["arranger"] = [", ".join(track.arrangers)]
            if track.lyricists:
                audio["lyricist"] = [", ".join(track.lyricists)]

            lyrics_text = track.lyrics_unsynced or track.lyrics_synced
            if lyrics_text:
                audio["lyrics"] = [lyrics_text]

            if effective_cover:
                audio.clear_pictures()
                pic = Picture()
                pic.data = effective_cover
                pic.type = 3
                pic.mime = "image/png" if effective_cover.startswith(b"\x89PNG") else "image/jpeg"
                pic.desc = "Cover"
                audio.add_picture(pic)

            audio.save()
            return True

        # 3. Opus (OggOpus Vorbis comments & metadata_block_picture)
        elif ext == ".opus":
            audio = OggOpus(str(path))
            if track.title:
                audio["title"] = [track.title]
            if track.artist:
                audio["artist"] = [track.artist]
            album_art = track.album_artist or track.artist
            if album_art:
                audio["albumartist"] = [album_art]
            if track.album:
                audio["album"] = [track.album]

            audio["tracknumber"] = [str(track.track_number)]
            tot = str(track.total_tracks or 1)
            audio["totaltracks"] = [tot]

            audio["discnumber"] = [str(track.disc_number or 1)]
            audio["totaldiscs"] = [str(track.total_discs or 1)]

            if track.year:
                audio["date"] = [str(track.year)]

            genre_val = track.genre or (", ".join(track.genres[:2]) if track.genres else "")
            if genre_val:
                audio["genre"] = [genre_val]

            if track.composers:
                audio["composer"] = [", ".join(track.composers)]
            if track.producers:
                audio["producer"] = [", ".join(track.producers)]
            if track.arrangers:
                audio["arranger"] = [", ".join(track.arrangers)]
            if track.lyricists:
                audio["lyricist"] = [", ".join(track.lyricists)]

            lyrics_text = track.lyrics_unsynced or track.lyrics_synced
            if lyrics_text:
                audio["lyrics"] = [lyrics_text]

            if effective_cover:
                p = Picture()
                p.data = effective_cover
                p.type = 3
                p.mime = "image/png" if effective_cover.startswith(b"\x89PNG") else "image/jpeg"
                p.desc = "Cover"
                audio["metadata_block_picture"] = [base64.b64encode(p.write()).decode("ascii")]

            audio.save()
            return True

        # 4. M4A / MP4
        elif ext == ".m4a":
            audio = MP4(str(path))
            if track.title:
                audio["\xa9nam"] = [track.title]
            if track.artist:
                audio["\xa9ART"] = [track.artist]
            album_art = track.album_artist or track.artist
            if album_art:
                audio["aART"] = [album_art]
            if track.album:
                audio["\xa9alb"] = [track.album]

            tot = track.total_tracks or 1
            audio["trkn"] = [(track.track_number, tot)]
            tot_discs = track.total_discs or 1
            audio["disk"] = [(track.disc_number, tot_discs)]

            if track.year:
                audio["\xa9day"] = [str(track.year)]

            genre_val = track.genre or (", ".join(track.genres[:2]) if track.genres else "")
            if genre_val:
                audio["\xa9gen"] = [genre_val]

            if track.composers:
                audio["\xa9wrt"] = [", ".join(track.composers)]

            if track.producers:
                audio["----:com.apple.iTunes:PRODUCER"] = [", ".join(track.producers).encode("utf-8")]

            lyrics_text = track.lyrics_unsynced or track.lyrics_synced
            if lyrics_text:
                audio["\xa9lyr"] = [lyrics_text]

            if effective_cover:
                cov_fmt = (
                    MP4Cover.FORMAT_PNG
                    if effective_cover.startswith(b"\x89PNG")
                    else MP4Cover.FORMAT_JPEG
                )
                audio["covr"] = [MP4Cover(effective_cover, imageformat=cov_fmt)]

            audio.save()
            return True

    except Exception as e:
        logger.warning(f"Error applying unified metadata to {path.name}: {e}")
        return False

    return False


# =====================================================================
# APPLY UNIFIED METADATA TO FULL ALBUM FOLDER
# =====================================================================

def apply_unified_metadata_to_album(
    folder: Union[str, Path],
    album_meta: Union[UnifiedAlbumMetadata, Dict[str, Any]],
    status_updater: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Applies a user-chosen or recommended unified album metadata payload across an entire album directory."""
    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise FileNotFoundError(f"Folder does not exist: {folder_path}")

    if isinstance(album_meta, dict):
        album = UnifiedAlbumMetadata.from_dict(album_meta)
    else:
        album = album_meta

    files = sorted([f for f in folder_path.iterdir() if f.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS])
    if not files:
        return {"album": album.album, "artist": album.artist, "year": album.year, "genre": album.genre}

    # Acquire artwork bytes if not yet loaded
    cover_bytes = album.cover_bytes
    if not cover_bytes and album.cover_url:
        try:
            req = urllib.request.Request(album.cover_url, headers={"User-Agent": "AuraHub/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                cover_bytes = r.read()
                album.cover_bytes = cover_bytes
        except Exception as e:
            logger.debug(f"Failed to fetch album cover URL: {e}")

    # Fallback to local cover if exists
    if not cover_bytes:
        try:
            from services.library_browser import extract_embedded_cover, find_cover_file
            found = find_cover_file(folder_path)
            if found and found.is_file():
                cover_bytes = found.read_bytes()
            else:
                embedded = extract_embedded_cover(folder_path)
                if embedded:
                    cover_bytes = embedded[0]
        except Exception:
            pass

    # Save loose cover.jpg for Navidrome
    if cover_bytes:
        write_loose_cover(folder_path, cover_bytes)

    if status_updater:
        status_updater("⚡ `[3/4]` *Writing audio tags across tracks...*")

    effective_genre = resolve_fallback_genre(album.artist, album.album, album.genre)
    aligned_pairs = align_album_tracks_globally(files, album.tracks)

    for file_path, matched_track in aligned_pairs:
        if not matched_track.album:
            matched_track.album = album.album
        if not matched_track.artist:
            matched_track.artist = album.artist
        if not matched_track.album_artist:
            matched_track.album_artist = album.album_artist or album.artist
        if not matched_track.total_tracks:
            matched_track.total_tracks = album.total_tracks or len(files)
        if not matched_track.year:
            matched_track.year = album.year
        if not matched_track.genre:
            matched_track.genre = effective_genre
        if not matched_track.producers and album.producers:
            matched_track.producers = album.producers
        if not matched_track.composers and album.composers:
            matched_track.composers = album.composers

        apply_unified_metadata_to_file(file_path, matched_track, cover_bytes)

        # Write or sync companion .lrc file
        lrc_path = file_path.with_suffix(".lrc")
        if matched_track.lyrics_synced and not lrc_path.exists():
            try:
                lrc_path.write_text(matched_track.lyrics_synced, encoding="utf-8")
            except Exception:
                pass
        elif not lrc_path.exists():
            fetch_and_save_lrc(matched_track.title, matched_track.artist, lrc_path)

    return {
        "album": album.album,
        "artist": album.artist,
        "year": album.year,
        "genre": effective_genre,
        "cover_bytes": cover_bytes,
    }


def align_album_tracks_globally(
    files: List[Path],
    candidate_tracks: List[UnifiedTrackMetadata],
) -> List[Tuple[Path, UnifiedTrackMetadata]]:
    """Optimally matches audio files to candidate tracks globally across duration, title, and position.

    Eliminates greedy track starvation, index inversion, and track order shuffling.
    """
    if not candidate_tracks:
        return [(f, UnifiedTrackMetadata(title=f.stem, track_number=idx)) for idx, f in enumerate(files, 1)]

    # 1. Extract file features
    file_features = []
    for idx_f, f in enumerate(files, 1):
        num_match = re.match(r"^(\d+)\s*[-.]\s*(.*)", f.stem)
        seq = int(num_match.group(1)) if num_match else idx_f
        raw_title = num_match.group(2).strip() if num_match else f.stem

        dur = 0.0
        existing_title = raw_title
        try:
            af = mutagen.File(str(f))
            if af and getattr(af, "info", None) and hasattr(af.info, "length"):
                dur = float(af.info.length)
            if af:
                if "TIT2" in af and af["TIT2"].text:
                    existing_title = str(af["TIT2"].text[0])
                elif "title" in af and af["title"]:
                    existing_title = str(af["title"][0])
                elif "\xa9nam" in af and af["\xa9nam"]:
                    existing_title = str(af["\xa9nam"][0])
        except Exception:
            pass

        file_features.append({
            "path": f,
            "seq": seq,
            "raw_title": raw_title,
            "tagged_title": existing_title,
            "duration": dur,
        })

    # 2. Build full pair score matrix
    all_pairs: List[Tuple[float, int, int]] = []
    for i, f_feat in enumerate(file_features):
        for j, c_trk in enumerate(candidate_tracks):
            score = 0.0

            # A. Duration proximity scoring
            if f_feat["duration"] > 0 and c_trk.duration_seconds > 0:
                diff = abs(f_feat["duration"] - c_trk.duration_seconds)
                if diff <= 2.5:
                    score += 120.0
                elif diff <= 5.0:
                    score += 80.0
                elif diff <= 9.0:
                    score += 40.0
                elif diff <= 15.0:
                    score += 10.0
                else:
                    score -= 40.0

            # B. Title & phonetic similarity scoring
            title_sim = max(
                calculate_string_similarity(f_feat["raw_title"], c_trk.title),
                calculate_string_similarity(f_feat["tagged_title"], c_trk.title),
                calculate_best_variant_similarity(franco_to_arabic(f_feat["raw_title"]), c_trk.title),
                calculate_best_variant_similarity(franco_to_arabic(f_feat["tagged_title"]), c_trk.title),
            )
            if title_sim >= 0.75:
                score += 120.0 * title_sim
            elif title_sim >= 0.45:
                score += 80.0 * title_sim
            elif title_sim >= 0.2:
                score += 30.0 * title_sim

            # C. Sequence position bonus
            if f_feat["seq"] == c_trk.track_number:
                if score > 0:
                    score += 60.0
                else:
                    score += 35.0

            all_pairs.append((score, i, j))

    # 3. Sort pairs descending and greedily match without conflict
    all_pairs.sort(key=lambda x: x[0], reverse=True)
    assigned_files: Set[int] = set()
    assigned_tracks: Set[int] = set()
    matches: Dict[int, int] = {}

    for score, i, j in all_pairs:
        if score < 25.0:
            break
        if i not in assigned_files and j not in assigned_tracks:
            assigned_files.add(i)
            assigned_tracks.add(j)
            matches[i] = j

    # 4. Fallback for unassigned files (assign to remaining tracks in sequence)
    unassigned_track_indices = [j for j in range(len(candidate_tracks)) if j not in assigned_tracks]
    for i in range(len(file_features)):
        if i not in matches:
            if unassigned_track_indices:
                j = unassigned_track_indices.pop(0)
                matches[i] = j
            else:
                matches[i] = None

    # 5. Format results
    aligned: List[Tuple[Path, UnifiedTrackMetadata]] = []
    for i, f_feat in enumerate(file_features):
        j = matches.get(i)
        if j is not None and j < len(candidate_tracks):
            aligned.append((f_feat["path"], candidate_tracks[j]))
        else:
            fallback_track = UnifiedTrackMetadata(
                title=f_feat["raw_title"],
                track_number=f_feat["seq"],
                total_tracks=len(files),
            )
            aligned.append((f_feat["path"], fallback_track))

    return aligned


# =====================================================================
# HYBRID ALBUM & PLAYLIST TAGGER (BACKWARD COMPATIBLE WITH PIPELINES)
# =====================================================================

def tag_album_hybrid(
    folder: Union[str, Path],
    album_name: str,
    artist_name: str,
    genius_raw: str = "",
    parsed_genius: Optional[Dict[str, Any]] = None,
    status_updater: Optional[Callable[[str], None]] = None,
    chosen_metadata: Optional[Union[UnifiedAlbumMetadata, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Applies multi-stage metadata tagging across all audio files in an album folder.

    If chosen_metadata is provided, applies the user-selected metadata payload directly.
    Otherwise, executes the MusicBrainz + AcoustID + Genius + Cover Art Archive pipeline.
    """
    if chosen_metadata is not None:
        return apply_unified_metadata_to_album(folder, chosen_metadata, status_updater)

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

    if not cover_bytes:
        try:
            from services.library_browser import extract_embedded_cover, find_cover_file
            existing_img = find_cover_file(folder_path)
            if existing_img and existing_img.is_file():
                cover_bytes = existing_img.read_bytes()
            else:
                extracted = extract_embedded_cover(folder_path)
                if extracted:
                    cover_bytes = extracted[0]
        except Exception as e:
            logger.debug(f"Could not extract local album cover: {e}")

    if cover_bytes:
        write_loose_cover(folder_path, cover_bytes)

    total_tracks = len(files)
    if mb_data and mb_data.get("tracks"):
        total_tracks = max(len(files), len(mb_data["tracks"]))
    elif genius_album and hasattr(genius_album, "tracks"):
        total_tracks = max(len(files), len(genius_album.tracks))

    assigned_positions: Set[int] = set()
    mb_tracklist = mb_data.get("tracks", []) if mb_data else []

    mb_genre = mb_data.get("genre") if mb_data else None
    effective_genre = resolve_fallback_genre(effective_artist, resolved_album, mb_genre)

    for idx_file, file_path in enumerate(files, 1):
        num_match = re.match(r"^(\d+)\s*[-.]\s*(.*)", file_path.stem)
        if num_match:
            file_seq = int(num_match.group(1))
            local_title = num_match.group(2).strip()
        else:
            file_seq = idx_file
            local_title = file_path.stem

        matched_title, final_track_num = find_best_track_match(
            file_path, local_title, file_seq, mb_tracklist, assigned_positions
        )

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

            if effective_genre:
                audio.delall("TCON")
                audio.add(TCON(encoding=3, text=effective_genre))

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

                if effective_genre:
                    audio["genre"] = [effective_genre]

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

        # Tag FLAC files with native Vorbis comments & Picture
        elif f_ext == ".flac":
            try:
                audio = FLAC(str(file_path))
                audio["title"] = [matched_title]

                if effective_artist and effective_artist.lower() not in ("unknown artist", "various"):
                    audio["artist"] = [effective_artist]
                    audio["albumartist"] = [effective_artist]

                audio["album"] = [resolved_album]
                audio["tracknumber"] = [str(final_track_num)]
                audio["tracktotal"] = [str(total_tracks)]
                audio["totaltracks"] = [str(total_tracks)]
                audio["discnumber"] = ["1"]
                audio["disctotal"] = ["1"]
                audio["totaldiscs"] = ["1"]

                if mb_data and mb_data.get("date"):
                    audio["date"] = [str(mb_data["date"])]

                if effective_genre:
                    audio["genre"] = [effective_genre]

                if lyrics_text:
                    audio["lyrics"] = [lyrics_text]

                if prods:
                    audio["producer"] = [", ".join(prods)]

                if writers:
                    audio["composer"] = [", ".join(writers)]

                if cover_bytes:
                    audio.clear_pictures()
                    pic = Picture()
                    pic.data = cover_bytes
                    pic.type = 3
                    pic.mime = "image/jpeg"
                    pic.desc = "Cover"
                    audio.add_picture(pic)

                audio.save()
            except Exception as flac_err:
                logger.warning(f"Failed to tag FLAC track {file_path.name}: {flac_err}")

        # Tag M4A files
        elif f_ext == ".m4a":
            try:
                audio = MP4(str(file_path))
                audio["\xa9nam"] = [matched_title]

                if effective_artist and effective_artist.lower() not in ("unknown artist", "various"):
                    audio["\xa9ART"] = [effective_artist]
                    audio["aART"] = [effective_artist]

                audio["\xa9alb"] = [resolved_album]
                audio["trkn"] = [(final_track_num, total_tracks)]
                audio["disk"] = [(1, 1)]

                if mb_data and mb_data.get("date"):
                    audio["\xa9day"] = [str(mb_data["date"])]

                if effective_genre:
                    audio["\xa9gen"] = [effective_genre]

                if lyrics_text:
                    audio["\xa9lyr"] = [lyrics_text]

                if writers:
                    audio["\xa9wrt"] = [", ".join(writers)]

                if cover_bytes:
                    cov_fmt = (
                        MP4Cover.FORMAT_PNG
                        if cover_bytes.startswith(b"\x89PNG")
                        else MP4Cover.FORMAT_JPEG
                    )
                    audio["covr"] = [MP4Cover(cover_bytes, imageformat=cov_fmt)]

                audio.save()
            except Exception as m4a_err:
                logger.warning(f"Failed to tag M4A track {file_path.name}: {m4a_err}")

    return {
        "album": resolved_album,
        "artist": effective_artist,
        "year": (mb_data.get("date") if mb_data else None) or "",
        "genre": effective_genre,
        "cover_bytes": cover_bytes,
    }



def tag_playlist_hybrid(
    folder: Union[str, Path],
    status_updater: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Tags individual tracks in a custom playlist folder supporting MP3, Opus, FLAC, and M4A."""
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

        elif f_ext == ".flac":
            try:
                audio = FLAC(str(file_path))
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
                logger.warning(f"Failed to tag playlist FLAC track {file_path.name}: {e}")

        elif f_ext == ".m4a":
            try:
                audio = MP4(str(file_path))
                audio["\xa9nam"] = [resolved_title]
                if resolved_artist:
                    audio["\xa9ART"] = [resolved_artist]
                    audio["aART"] = [resolved_artist]
                if resolved_genre:
                    audio["\xa9gen"] = [resolved_genre]
                if lyrics_text:
                    audio["\xa9lyr"] = [lyrics_text]
                audio.save()
            except Exception as e:
                logger.warning(f"Failed to tag playlist M4A track {file_path.name}: {e}")

    return {
        "album": "Custom Playlist",
        "artist": "Various Artists",
        "year": "",
        "genre": "Mixed",
        "cover_bytes": None,
    }
