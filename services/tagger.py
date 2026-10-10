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
# METADATA STUDIO GRANULAR TAG INSPECTION & WRITING
# =====================================================================

def extract_cover_bytes(file_path: Union[str, Path]) -> Optional[Tuple[bytes, str]]:
    """Extracts binary cover artwork and MIME type from an audio file or album directory."""
    path = Path(file_path)
    if path.is_dir():
        for cov_name in ("cover.jpg", "cover.png", "folder.jpg", "folder.png"):
            cov_file = path / cov_name
            if cov_file.is_file() and cov_file.stat().st_size > 0:
                mime = "image/png" if cov_file.suffix.lower() == ".png" else "image/jpeg"
                return cov_file.read_bytes(), mime
        # Fallback to inspecting first audio file in directory
        audio_files = sorted(
            [f for f in path.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS],
            key=lambda x: x.name,
        )
        for af in audio_files:
            res = extract_cover_bytes(af)
            if res:
                return res
        return None

    if not path.is_file():
        return None

    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            try:
                id3 = ID3(str(path))
                apics = id3.getall("APIC")
                if apics:
                    pic = apics[0]
                    mime = getattr(pic, "mime", "image/jpeg") or "image/jpeg"
                    return pic.data, mime
            except ID3NoHeaderError:
                pass
        elif ext == ".flac":
            fl = FLAC(str(path))
            if fl.pictures:
                pic = fl.pictures[0]
                return pic.data, pic.mime or "image/jpeg"
        elif ext == ".opus":
            op = OggOpus(str(path))
            mbp = op.get("metadata_block_picture")
            if mbp:
                pic_data = base64.b64decode(mbp[0])
                p = Picture(pic_data)
                return p.data, p.mime or "image/jpeg"
        elif ext == ".m4a":
            mp = MP4(str(path))
            covrs = mp.get("covr")
            if covrs:
                c = covrs[0]
                mime = "image/png" if getattr(c, "imageformat", None) == MP4Cover.FORMAT_PNG else "image/jpeg"
                return bytes(c), mime
    except Exception as e:
        logger.debug(f"Failed to extract cover from {path.name}: {e}")

    # Fallback to sibling loose cover if exists
    for cov_name in ("cover.jpg", "cover.png", "folder.jpg", "folder.png"):
        cov_file = path.parent / cov_name
        if cov_file.is_file() and cov_file.stat().st_size > 0:
            mime = "image/png" if cov_file.suffix.lower() == ".png" else "image/jpeg"
            return cov_file.read_bytes(), mime

    return None


def read_tags(file_path: Union[str, Path]) -> Dict[str, Any]:
    """Reads comprehensive ID3, Vorbis, or MP4 tags, lyrics, and artwork metadata.

    Supports individual files and whole album folders.
    """
    path = Path(file_path)
    if path.is_dir():
        audio_files = sorted(
            [f for f in path.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS],
            key=lambda x: x.name,
        )
        tracks = [read_tags(f) for f in audio_files]

        album_name = path.name
        artist_name = path.parent.name if path.parent != config.BASE_DOWNLOAD_DIR else ""
        album_artist_name = artist_name
        year = ""
        genre = ""
        composers: List[str] = []
        producers: List[str] = []

        for t in tracks:
            if t.get("album") and (not album_name or album_name == path.name):
                album_name = t["album"]
            if t.get("artist") and not artist_name:
                artist_name = t["artist"]
            if t.get("album_artist") and not album_artist_name:
                album_artist_name = t["album_artist"]
            if t.get("year") and not year:
                year = t["year"]
            if t.get("genre") and not genre:
                genre = t["genre"]
            if t.get("composers") and not composers:
                composers = t["composers"]
            if t.get("producers") and not producers:
                producers = t["producers"]

        has_loose_cover = any(
            (path / c).is_file() and (path / c).stat().st_size > 0
            for c in ("cover.jpg", "cover.png", "folder.jpg", "folder.png")
        )

        return {
            "path": str(path),
            "folder": str(path.name),
            "is_dir": True,
            "album": album_name,
            "artist": artist_name,
            "album_artist": album_artist_name or artist_name,
            "year": year,
            "genre": genre,
            "composers": composers,
            "producers": producers,
            "has_cover": has_loose_cover or any(t.get("has_cover") for t in tracks),
            "has_loose_cover": has_loose_cover,
            "track_count": len(tracks),
            "lrc_count": sum(1 for t in tracks if t.get("has_lrc")),
            "tracks": tracks,
        }

    ext = path.suffix.lower()
    info: Dict[str, Any] = {
        "path": str(path),
        "filename": path.name,
        "format": ext.lstrip("."),
        "is_dir": False,
        "title": path.stem,
        "artist": "",
        "album_artist": "",
        "album": "",
        "year": "",
        "date": "",
        "track_number": 1,
        "total_tracks": 1,
        "disc_number": 1,
        "total_discs": 1,
        "genre": "",
        "composers": [],
        "producers": [],
        "arrangers": [],
        "lyricists": [],
        "lyrics_unsynced": "",
        "lyrics_synced": "",
        "duration_seconds": 0.0,
        "has_cover": False,
        "cover_mime": "",
        "cover_size": 0,
        "has_lrc": path.with_suffix(".lrc").is_file(),
    }

    try:
        mut_file = mutagen.File(str(path))
        if mut_file and getattr(mut_file, "info", None) and hasattr(mut_file.info, "length"):
            info["duration_seconds"] = round(float(mut_file.info.length), 1)
    except Exception:
        pass

    # Check companion .lrc
    lrc_file = path.with_suffix(".lrc")
    if lrc_file.is_file():
        try:
            info["lyrics_synced"] = lrc_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass

    try:
        if ext == ".mp3":
            try:
                id3 = ID3(str(path))
                if "TIT2" in id3 and id3["TIT2"].text: info["title"] = str(id3["TIT2"].text[0])
                if "TPE1" in id3 and id3["TPE1"].text: info["artist"] = str(id3["TPE1"].text[0])
                if "TPE2" in id3 and id3["TPE2"].text: info["album_artist"] = str(id3["TPE2"].text[0])
                if "TALB" in id3 and id3["TALB"].text: info["album"] = str(id3["TALB"].text[0])
                if "TDRC" in id3 and id3["TDRC"].text:
                    info["date"] = str(id3["TDRC"].text[0])
                    info["year"] = info["date"][:4]
                if "TCON" in id3 and id3["TCON"].text: info["genre"] = str(id3["TCON"].text[0])
                if "TRCK" in id3 and id3["TRCK"].text:
                    trck = str(id3["TRCK"].text[0])
                    if "/" in trck:
                        p = trck.split("/", 1)
                        if p[0].isdigit(): info["track_number"] = int(p[0])
                        if p[1].isdigit(): info["total_tracks"] = int(p[1])
                    elif trck.isdigit():
                        info["track_number"] = int(trck)
                if "TPOS" in id3 and id3["TPOS"].text:
                    tpos = str(id3["TPOS"].text[0])
                    if "/" in tpos:
                        p = tpos.split("/", 1)
                        if p[0].isdigit(): info["disc_number"] = int(p[0])
                        if p[1].isdigit(): info["total_discs"] = int(p[1])
                    elif tpos.isdigit():
                        info["disc_number"] = int(tpos)
                if "TCOM" in id3 and id3["TCOM"].text:
                    info["composers"] = [c.strip() for c in str(id3["TCOM"].text[0]).split(",") if c.strip()]
                if "TEXT" in id3 and id3["TEXT"].text:
                    info["lyricists"] = [l.strip() for l in str(id3["TEXT"].text[0]).split(",") if l.strip()]
                for txxx in id3.getall("TXXX"):
                    d_upper = txxx.desc.upper()
                    if d_upper == "PRODUCER":
                        info["producers"] = [p.strip() for p in str(txxx.text[0]).split(",") if p.strip()]
                    elif d_upper == "ARRANGER":
                        info["arrangers"] = [a.strip() for a in str(txxx.text[0]).split(",") if a.strip()]
                if not info["producers"]:
                    for ipls in id3.getall("IPLS"):
                        for role, person in getattr(ipls, "people", []):
                            if "producer" in str(role).lower() and person:
                                info["producers"].extend([p.strip() for p in str(person).split(",") if p.strip()])
                uslts = id3.getall("USLT")
                if uslts:
                    info["lyrics_unsynced"] = str(uslts[0].text)
                apics = id3.getall("APIC")
                if apics:
                    info["has_cover"] = True
                    info["cover_mime"] = getattr(apics[0], "mime", "image/jpeg") or "image/jpeg"
                    info["cover_size"] = len(apics[0].data) if hasattr(apics[0], "data") else 0
            except ID3NoHeaderError:
                pass

        elif ext == ".flac":
            fl = FLAC(str(path))
            if "title" in fl and fl["title"]: info["title"] = fl["title"][0]
            if "artist" in fl and fl["artist"]: info["artist"] = fl["artist"][0]
            if "albumartist" in fl and fl["albumartist"]: info["album_artist"] = fl["albumartist"][0]
            if "album" in fl and fl["album"]: info["album"] = fl["album"][0]
            if "date" in fl and fl["date"]:
                info["date"] = fl["date"][0]
                info["year"] = info["date"][:4]
            if "genre" in fl and fl["genre"]: info["genre"] = fl["genre"][0]
            if "tracknumber" in fl and fl["tracknumber"] and fl["tracknumber"][0].isdigit():
                info["track_number"] = int(fl["tracknumber"][0])
            tot = fl.get("totaltracks") or fl.get("tracktotal")
            if tot and tot[0].isdigit():
                info["total_tracks"] = int(tot[0])
            if "discnumber" in fl and fl["discnumber"] and fl["discnumber"][0].isdigit():
                info["disc_number"] = int(fl["discnumber"][0])
            tot_d = fl.get("totaldiscs") or fl.get("disctotal")
            if tot_d and tot_d[0].isdigit():
                info["total_discs"] = int(tot_d[0])
            if "composer" in fl: info["composers"] = list(fl["composer"])
            if "producer" in fl: info["producers"] = list(fl["producer"])
            if "arranger" in fl: info["arrangers"] = list(fl["arranger"])
            if "lyricist" in fl: info["lyricists"] = list(fl["lyricist"])
            if "lyrics" in fl and fl["lyrics"]:
                info["lyrics_unsynced"] = fl["lyrics"][0]
            if fl.pictures:
                info["has_cover"] = True
                info["cover_mime"] = fl.pictures[0].mime or "image/jpeg"
                info["cover_size"] = len(fl.pictures[0].data)

        elif ext == ".opus":
            op = OggOpus(str(path))
            if "title" in op and op["title"]: info["title"] = op["title"][0]
            if "artist" in op and op["artist"]: info["artist"] = op["artist"][0]
            if "albumartist" in op and op["albumartist"]: info["album_artist"] = op["albumartist"][0]
            if "album" in op and op["album"]: info["album"] = op["album"][0]
            if "date" in op and op["date"]:
                info["date"] = op["date"][0]
                info["year"] = info["date"][:4]
            if "genre" in op and op["genre"]: info["genre"] = op["genre"][0]
            if "tracknumber" in op and op["tracknumber"] and op["tracknumber"][0].isdigit():
                info["track_number"] = int(op["tracknumber"][0])
            tot = op.get("totaltracks") or op.get("tracktotal")
            if tot and tot[0].isdigit():
                info["total_tracks"] = int(tot[0])
            if "discnumber" in op and op["discnumber"] and op["discnumber"][0].isdigit():
                info["disc_number"] = int(op["discnumber"][0])
            tot_d = op.get("totaldiscs") or op.get("disctotal")
            if tot_d and tot_d[0].isdigit():
                info["total_discs"] = int(tot_d[0])
            if "composer" in op: info["composers"] = list(op["composer"])
            if "producer" in op: info["producers"] = list(op["producer"])
            if "arranger" in op: info["arrangers"] = list(op["arranger"])
            if "lyricist" in op: info["lyricists"] = list(op["lyricist"])
            if "lyrics" in op and op["lyrics"]:
                info["lyrics_unsynced"] = op["lyrics"][0]
            mbp = op.get("metadata_block_picture")
            if mbp:
                try:
                    p = Picture(base64.b64decode(mbp[0]))
                    info["has_cover"] = True
                    info["cover_mime"] = p.mime or "image/jpeg"
                    info["cover_size"] = len(p.data)
                except Exception:
                    info["has_cover"] = True

        elif ext == ".m4a":
            mp = MP4(str(path))
            if "\xa9nam" in mp and mp["\xa9nam"]: info["title"] = mp["\xa9nam"][0]
            if "\xa9ART" in mp and mp["\xa9ART"]: info["artist"] = mp["\xa9ART"][0]
            if "aART" in mp and mp["aART"]: info["album_artist"] = mp["aART"][0]
            if "\xa9alb" in mp and mp["\xa9alb"]: info["album"] = mp["\xa9alb"][0]
            if "\xa9day" in mp and mp["\xa9day"]:
                info["date"] = str(mp["\xa9day"][0])
                info["year"] = info["date"][:4]
            if "\xa9gen" in mp and mp["\xa9gen"]: info["genre"] = mp["\xa9gen"][0]
            if "trkn" in mp and mp["trkn"]:
                info["track_number"] = mp["trkn"][0][0]
                info["total_tracks"] = mp["trkn"][0][1]
            if "disk" in mp and mp["disk"]:
                info["disc_number"] = mp["disk"][0][0]
                info["total_discs"] = mp["disk"][0][1]
            if "\xa9wrt" in mp and mp["\xa9wrt"]:
                info["composers"] = [c.strip() for c in str(mp["\xa9wrt"][0]).split(",") if c.strip()]
            prod = mp.get("----:com.apple.iTunes:PRODUCER")
            if prod:
                try:
                    info["producers"] = [p.strip() for p in prod[0].decode("utf-8").split(",") if p.strip()]
                except Exception:
                    pass
            if "\xa9lyr" in mp and mp["\xa9lyr"]:
                info["lyrics_unsynced"] = mp["\xa9lyr"][0]
            if "covr" in mp and mp["covr"]:
                c = mp["covr"][0]
                info["has_cover"] = True
                info["cover_mime"] = "image/png" if getattr(c, "imageformat", None) == MP4Cover.FORMAT_PNG else "image/jpeg"
                info["cover_size"] = len(bytes(c))

    except Exception as e:
        logger.warning(f"Error parsing audio tags for {path.name}: {e}")

    return info


def write_tags(
    file_path: Union[str, Path],
    fields: Dict[str, Any],
    cover_bytes: Optional[bytes] = None,
) -> bool:
    """Updates only the supplied fields cleanly using Mutagen without stripping unedited tags.

    Handles single tracks and batch album folder operations.
    """
    path = Path(file_path)
    if not path.exists():
        logger.warning(f"write_tags target does not exist: {path}")
        return False

    # 1. Directory / Album batch operation
    if path.is_dir():
        if cover_bytes:
            write_loose_cover(path, cover_bytes)

        audio_files = sorted(
            [f for f in path.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS],
            key=lambda x: x.name,
        )
        if not audio_files:
            return True

        tracks_data = fields.get("tracks")
        if isinstance(tracks_data, list) and len(tracks_data) > 0:
            file_map = {f.name: f for f in audio_files}
            all_ok = True
            for idx, t_dict in enumerate(tracks_data):
                t_filename = t_dict.get("filename")
                target_f = file_map.get(t_filename) if t_filename else (audio_files[idx] if idx < len(audio_files) else None)
                if target_f:
                    merged = dict(fields)
                    merged.pop("tracks", None)
                    merged.update(t_dict)
                    ok = write_tags(target_f, merged, cover_bytes)
                    if not ok:
                        all_ok = False
            return all_ok

        all_ok = True
        common_fields = dict(fields)
        common_fields.pop("tracks", None)
        if len(audio_files) > 1:
            common_fields.pop("title", None)
            common_fields.pop("track_number", None)

        for af in audio_files:
            ok = write_tags(af, common_fields, cover_bytes)
            if not ok:
                all_ok = False
        return all_ok

    # 2. Single audio track update
    ext = path.suffix.lower()
    if ext not in SUPPORTED_AUDIO_EXTENSIONS:
        return False

    # Companion .lrc handling
    if "lyrics_synced" in fields and fields["lyrics_synced"] is not None:
        lrc_text = str(fields["lyrics_synced"]).strip()
        lrc_path = path.with_suffix(".lrc")
        if lrc_text:
            try:
                lrc_path.write_text(lrc_text, encoding="utf-8")
                logger.info(f"Updated companion .lrc for {path.name}")
            except Exception as e:
                logger.warning(f"Failed to write companion .lrc: {e}")
        elif lrc_path.is_file():
            try:
                lrc_path.unlink()
            except Exception:
                pass

    try:
        # A. MP3 (ID3v2.3)
        if ext == ".mp3":
            try:
                audio = ID3(str(path))
            except ID3NoHeaderError:
                audio = ID3()

            if "title" in fields and fields["title"] is not None:
                audio.delall("TIT2")
                if str(fields["title"]).strip():
                    audio.add(TIT2(encoding=3, text=str(fields["title"]).strip()))

            if "artist" in fields and fields["artist"] is not None:
                audio.delall("TPE1")
                if str(fields["artist"]).strip():
                    audio.add(TPE1(encoding=3, text=str(fields["artist"]).strip()))

            if "album_artist" in fields and fields["album_artist"] is not None:
                audio.delall("TPE2")
                if str(fields["album_artist"]).strip():
                    audio.add(TPE2(encoding=3, text=str(fields["album_artist"]).strip()))

            if "album" in fields and fields["album"] is not None:
                audio.delall("TALB")
                if str(fields["album"]).strip():
                    audio.add(TALB(encoding=3, text=str(fields["album"]).strip()))

            if ("track_number" in fields or "total_tracks" in fields):
                curr_trck = 1
                curr_tot = 1
                if "TRCK" in audio and audio["TRCK"].text:
                    raw_trck = str(audio["TRCK"].text[0])
                    if "/" in raw_trck:
                        p = raw_trck.split("/", 1)
                        if p[0].isdigit(): curr_trck = int(p[0])
                        if p[1].isdigit(): curr_tot = int(p[1])
                    elif raw_trck.isdigit():
                        curr_trck = int(raw_trck)
                num = fields.get("track_number", curr_trck)
                tot = fields.get("total_tracks", curr_tot)
                audio.delall("TRCK")
                audio.add(TRCK(encoding=3, text=f"{num}/{tot}"))

            if ("disc_number" in fields or "total_discs" in fields):
                curr_disc = 1
                curr_tot_d = 1
                if "TPOS" in audio and audio["TPOS"].text:
                    raw_tpos = str(audio["TPOS"].text[0])
                    if "/" in raw_tpos:
                        p = raw_tpos.split("/", 1)
                        if p[0].isdigit(): curr_disc = int(p[0])
                        if p[1].isdigit(): curr_tot_d = int(p[1])
                    elif raw_tpos.isdigit():
                        curr_disc = int(raw_tpos)
                num_d = fields.get("disc_number", curr_disc)
                tot_d = fields.get("total_discs", curr_tot_d)
                audio.delall("TPOS")
                audio.add(TPOS(encoding=3, text=f"{num_d}/{tot_d}"))

            if "year" in fields or "date" in fields:
                yr = str(fields.get("year") or fields.get("date") or "").strip()
                audio.delall("TDRC")
                if yr:
                    audio.add(TDRC(encoding=3, text=yr))

            if "genre" in fields and fields["genre"] is not None:
                audio.delall("TCON")
                g = str(fields["genre"]).strip()
                if g:
                    audio.add(TCON(encoding=3, text=g))

            if "composers" in fields and fields["composers"] is not None:
                audio.delall("TCOM")
                comps = fields["composers"]
                c_str = ", ".join(comps) if isinstance(comps, list) else str(comps).strip()
                if c_str:
                    audio.add(TCOM(encoding=3, text=c_str))

            if "lyricists" in fields and fields["lyricists"] is not None:
                audio.delall("TEXT")
                lyrs = fields["lyricists"]
                l_str = ", ".join(lyrs) if isinstance(lyrs, list) else str(lyrs).strip()
                if l_str:
                    audio.add(TEXT(encoding=3, text=l_str))

            if "producers" in fields and fields["producers"] is not None:
                prods = fields["producers"]
                p_str = ", ".join(prods) if isinstance(prods, list) else str(prods).strip()
                for frame in list(audio.getall("TXXX")):
                    if frame.desc.upper() == "PRODUCER":
                        audio.delall(frame.HashKey)
                audio.delall("IPLS")
                if p_str:
                    audio.add(TXXX(encoding=3, desc="PRODUCER", text=p_str))
                    audio.add(IPLS(encoding=3, people=[("producer", p_str)]))

            if "arrangers" in fields and fields["arrangers"] is not None:
                arrs = fields["arrangers"]
                a_str = ", ".join(arrs) if isinstance(arrs, list) else str(arrs).strip()
                for frame in list(audio.getall("TXXX")):
                    if frame.desc.upper() == "ARRANGER":
                        audio.delall(frame.HashKey)
                if a_str:
                    audio.add(TXXX(encoding=3, desc="ARRANGER", text=a_str))

            if "lyrics_unsynced" in fields and fields["lyrics_unsynced"] is not None:
                audio.delall("USLT")
                u_text = str(fields["lyrics_unsynced"]).strip()
                if u_text:
                    audio.add(USLT(encoding=3, lang="eng", desc="", text=u_text))

            if cover_bytes:
                audio.delall("APIC")
                mime = "image/png" if cover_bytes.startswith(b"\x89PNG") else "image/jpeg"
                audio.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover_bytes))

            audio.save(str(path), v2_version=3)
            return True

        # B. FLAC (Vorbis comments & Picture)
        elif ext == ".flac":
            audio = FLAC(str(path))
            if "title" in fields and fields["title"] is not None:
                audio["title"] = [str(fields["title"]).strip()]
            if "artist" in fields and fields["artist"] is not None:
                audio["artist"] = [str(fields["artist"]).strip()]
            if "album_artist" in fields and fields["album_artist"] is not None:
                audio["albumartist"] = [str(fields["album_artist"]).strip()]
            if "album" in fields and fields["album"] is not None:
                audio["album"] = [str(fields["album"]).strip()]
            if "track_number" in fields and fields["track_number"] is not None:
                audio["tracknumber"] = [str(fields["track_number"])]
            if "total_tracks" in fields and fields["total_tracks"] is not None:
                audio["totaltracks"] = [str(fields["total_tracks"])]
                audio["tracktotal"] = [str(fields["total_tracks"])]
            if "disc_number" in fields and fields["disc_number"] is not None:
                audio["discnumber"] = [str(fields["disc_number"])]
            if "total_discs" in fields and fields["total_discs"] is not None:
                audio["totaldiscs"] = [str(fields["total_discs"])]
                audio["disctotal"] = [str(fields["total_discs"])]
            if "year" in fields or "date" in fields:
                yr = str(fields.get("year") or fields.get("date") or "").strip()
                if yr:
                    audio["date"] = [yr]
                    audio["year"] = [yr[:4]]
            if "genre" in fields and fields["genre"] is not None:
                audio["genre"] = [str(fields["genre"]).strip()]
            if "composers" in fields and fields["composers"] is not None:
                comps = fields["composers"]
                audio["composer"] = comps if isinstance(comps, list) else [str(comps).strip()]
            if "producers" in fields and fields["producers"] is not None:
                prods = fields["producers"]
                audio["producer"] = prods if isinstance(prods, list) else [str(prods).strip()]
            if "arrangers" in fields and fields["arrangers"] is not None:
                arrs = fields["arrangers"]
                audio["arranger"] = arrs if isinstance(arrs, list) else [str(arrs).strip()]
            if "lyricists" in fields and fields["lyricists"] is not None:
                lyrs = fields["lyricists"]
                audio["lyricist"] = lyrs if isinstance(lyrs, list) else [str(lyrs).strip()]
            if "lyrics_unsynced" in fields and fields["lyrics_unsynced"] is not None:
                audio["lyrics"] = [str(fields["lyrics_unsynced"]).strip()]

            if cover_bytes:
                audio.clear_pictures()
                pic = Picture()
                pic.data = cover_bytes
                pic.type = 3
                pic.mime = "image/png" if cover_bytes.startswith(b"\x89PNG") else "image/jpeg"
                pic.desc = "Cover"
                audio.add_picture(pic)

            audio.save()
            return True

        # C. Opus (OggOpus Vorbis comments & metadata_block_picture)
        elif ext == ".opus":
            audio = OggOpus(str(path))
            if "title" in fields and fields["title"] is not None:
                audio["title"] = [str(fields["title"]).strip()]
            if "artist" in fields and fields["artist"] is not None:
                audio["artist"] = [str(fields["artist"]).strip()]
            if "album_artist" in fields and fields["album_artist"] is not None:
                audio["albumartist"] = [str(fields["album_artist"]).strip()]
            if "album" in fields and fields["album"] is not None:
                audio["album"] = [str(fields["album"]).strip()]
            if "track_number" in fields and fields["track_number"] is not None:
                audio["tracknumber"] = [str(fields["track_number"])]
            if "total_tracks" in fields and fields["total_tracks"] is not None:
                audio["totaltracks"] = [str(fields["total_tracks"])]
            if "disc_number" in fields and fields["disc_number"] is not None:
                audio["discnumber"] = [str(fields["disc_number"])]
            if "total_discs" in fields and fields["total_discs"] is not None:
                audio["totaldiscs"] = [str(fields["total_discs"])]
            if "year" in fields or "date" in fields:
                yr = str(fields.get("year") or fields.get("date") or "").strip()
                if yr:
                    audio["date"] = [yr]
            if "genre" in fields and fields["genre"] is not None:
                audio["genre"] = [str(fields["genre"]).strip()]
            if "composers" in fields and fields["composers"] is not None:
                comps = fields["composers"]
                audio["composer"] = comps if isinstance(comps, list) else [str(comps).strip()]
            if "producers" in fields and fields["producers"] is not None:
                prods = fields["producers"]
                audio["producer"] = prods if isinstance(prods, list) else [str(prods).strip()]
            if "arrangers" in fields and fields["arrangers"] is not None:
                arrs = fields["arrangers"]
                audio["arranger"] = arrs if isinstance(arrs, list) else [str(arrs).strip()]
            if "lyricists" in fields and fields["lyricists"] is not None:
                lyrs = fields["lyricists"]
                audio["lyricist"] = lyrs if isinstance(lyrs, list) else [str(lyrs).strip()]
            if "lyrics_unsynced" in fields and fields["lyrics_unsynced"] is not None:
                audio["lyrics"] = [str(fields["lyrics_unsynced"]).strip()]

            if cover_bytes:
                pic = Picture()
                pic.data = cover_bytes
                pic.type = 3
                pic.mime = "image/png" if cover_bytes.startswith(b"\x89PNG") else "image/jpeg"
                pic.desc = "Cover"
                audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]

            audio.save()
            return True

        # D. M4A / MP4
        elif ext == ".m4a":
            audio = MP4(str(path))
            if "title" in fields and fields["title"] is not None:
                audio["\xa9nam"] = [str(fields["title"]).strip()]
            if "artist" in fields and fields["artist"] is not None:
                audio["\xa9ART"] = [str(fields["artist"]).strip()]
            if "album_artist" in fields and fields["album_artist"] is not None:
                audio["aART"] = [str(fields["album_artist"]).strip()]
            if "album" in fields and fields["album"] is not None:
                audio["\xa9alb"] = [str(fields["album"]).strip()]
            if ("track_number" in fields or "total_tracks" in fields):
                curr_trkn = audio.get("trkn", [(1, 1)])[0]
                num = int(fields.get("track_number", curr_trkn[0]))
                tot = int(fields.get("total_tracks", curr_trkn[1]))
                audio["trkn"] = [(num, tot)]
            if ("disc_number" in fields or "total_discs" in fields):
                curr_disk = audio.get("disk", [(1, 1)])[0]
                num_d = int(fields.get("disc_number", curr_disk[0]))
                tot_d = int(fields.get("total_discs", curr_disk[1]))
                audio["disk"] = [(num_d, tot_d)]
            if "year" in fields or "date" in fields:
                yr = str(fields.get("year") or fields.get("date") or "").strip()
                if yr:
                    audio["\xa9day"] = [yr]
            if "genre" in fields and fields["genre"] is not None:
                audio["\xa9gen"] = [str(fields["genre"]).strip()]
            if "composers" in fields and fields["composers"] is not None:
                comps = fields["composers"]
                c_str = ", ".join(comps) if isinstance(comps, list) else str(comps).strip()
                if c_str:
                    audio["\xa9wrt"] = [c_str]
            if "producers" in fields and fields["producers"] is not None:
                prods = fields["producers"]
                p_str = ", ".join(prods) if isinstance(prods, list) else str(prods).strip()
                if p_str:
                    audio["----:com.apple.iTunes:PRODUCER"] = [p_str.encode("utf-8")]
            if "lyrics_unsynced" in fields and fields["lyrics_unsynced"] is not None:
                audio["\xa9lyr"] = [str(fields["lyrics_unsynced"]).strip()]

            if cover_bytes:
                cov_fmt = (
                    MP4Cover.FORMAT_PNG
                    if cover_bytes.startswith(b"\x89PNG")
                    else MP4Cover.FORMAT_JPEG
                )
                audio["covr"] = [MP4Cover(cover_bytes, imageformat=cov_fmt)]

            audio.save()
            return True

    except Exception as e:
        logger.warning(f"Failed to write tags to {path.name}: {e}")
        return False

    return False


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

    # Always ensure genre is normalized to English (never Arabic script)
    raw_genre = track.genre or (", ".join(track.genres[:2]) if track.genres else "")
    effective_genre = resolve_fallback_genre(track.artist, track.album, raw_genre)
    track.genre = effective_genre

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

            if effective_genre:
                audio.delall("TCON")
                audio.add(TCON(encoding=3, text=effective_genre))

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

            if effective_genre:
                audio["genre"] = [effective_genre]

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

            if effective_genre:
                audio["genre"] = [effective_genre]

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

            if effective_genre:
                audio["\xa9gen"] = [effective_genre]

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
        matched_track.genre = resolve_fallback_genre(
            matched_track.artist or album.artist,
            matched_track.album or album.album,
            matched_track.genre or effective_genre,
        )
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
        resolved_genre = resolve_fallback_genre(
            resolved_artist, resolved_title, mb_trk.get("genre") or ""
        )

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
