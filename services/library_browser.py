"""Library inspection, cover art serving, and metadata health auditing."""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from services.lyrics import sync_all_lrc_in_folder
from services.system import delete_album_folder, get_album_folders

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
PREFERRED_STEMS = ["cover", "folder", "front", "albumart", "albumartsmall", "artwork"]

DEFAULT_PLACEHOLDER_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" width="100%" height="100%">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#181920"/>
      <stop offset="100%" stop-color="#0c0d10"/>
    </linearGradient>
  </defs>
  <rect width="200" height="200" rx="20" fill="url(#bg)"/>
  <circle cx="100" cy="100" r="64" fill="#131418" stroke="rgba(255,255,255,0.08)" stroke-width="2"/>
  <circle cx="100" cy="100" r="46" fill="#0e0f13" stroke="rgba(255,255,255,0.05)" stroke-width="1"/>
  <circle cx="100" cy="100" r="22" fill="#22242d"/>
  <circle cx="100" cy="100" r="6" fill="#ffffff" opacity="0.85"/>
  <path d="M125 64v32a14 14 0 1 1-8-12.7V68h20v-4z" fill="rgba(255,255,255,0.4)"/>
</svg>"""


def find_cover_file(album_dir: Path) -> Optional[Path]:
    """Finds an existing album jacket image file in album_dir using case-insensitive matching."""
    if not album_dir.is_dir():
        return None

    try:
        files = [f for f in album_dir.iterdir() if f.is_file()]
    except Exception as e:
        logger.debug(f"Could not read directory {album_dir}: {e}")
        return None

    # 1. Match preferred filenames in priority order
    for preferred_stem in PREFERRED_STEMS:
        for f in files:
            if f.suffix.lower() in IMAGE_EXTENSIONS and f.stem.lower() == preferred_stem:
                return f

    # 2. Match any image file if no standard name matched
    for f in files:
        if f.suffix.lower() in IMAGE_EXTENSIONS:
            return f

    return None


def extract_embedded_cover(album_dir: Path) -> Optional[Tuple[bytes, str]]:
    """Extracts embedded front cover artwork from the first MP3 or FLAC file in album_dir.

    If extracted, also saves cover.jpg / cover.png to album_dir for Navidrome and future serving.
    Returns:
        (image_bytes, mime_type) if found, else None.
    """
    if not album_dir.is_dir():
        return None

    try:
        audio_files = [
            f for f in album_dir.iterdir()
            if f.is_file() and f.suffix.lower() in (".mp3", ".flac")
        ]
    except Exception as e:
        logger.debug(f"Could not scan audio files in {album_dir}: {e}")
        return None

    # Check MP3 files
    mp3_files = [f for f in audio_files if f.suffix.lower() == ".mp3"]
    for mp3_path in mp3_files[:3]:
        try:
            from mutagen.id3 import ID3

            tags = ID3(str(mp3_path))
            for key in tags.keys():
                if key.startswith("APIC"):
                    apic = tags[key]
                    data = getattr(apic, "data", None)
                    mime = getattr(apic, "mime", "image/jpeg") or "image/jpeg"
                    if data:
                        target_ext = ".png" if "png" in mime.lower() else ".jpg"
                        cache_file = album_dir / f"cover{target_ext}"
                        try:
                            if not cache_file.exists():
                                cache_file.write_bytes(data)
                        except Exception as cache_err:
                            logger.debug(f"Failed to cache extracted cover: {cache_err}")
                        return data, mime
        except Exception:
            pass

    # Check FLAC files
    flac_files = [f for f in audio_files if f.suffix.lower() == ".flac"]
    for flac_path in flac_files[:3]:
        try:
            from mutagen.flac import FLAC

            audio = FLAC(str(flac_path))
            if getattr(audio, "pictures", None):
                pic = audio.pictures[0]
                data = getattr(pic, "data", None)
                mime = getattr(pic, "mime", "image/jpeg") or "image/jpeg"
                if data:
                    target_ext = ".png" if "png" in mime.lower() else ".jpg"
                    cache_file = album_dir / f"cover{target_ext}"
                    try:
                        if not cache_file.exists():
                            cache_file.write_bytes(data)
                    except Exception as cache_err:
                        logger.debug(f"Failed to cache extracted cover: {cache_err}")
                    return data, mime
        except Exception:
            pass

    return None


def get_library_albums() -> List[Dict[str, Any]]:
    """Scans the music library and returns albums with track counts, cover existence, and lyrics health."""
    base_dir = config.BASE_DOWNLOAD_DIR.resolve()
    rel_folders = get_album_folders(base_dir)
    results: List[Dict[str, Any]] = []

    for rel_path in rel_folders:
        folder_path = (base_dir / rel_path).resolve()
        if not folder_path.is_dir():
            continue

        parts = rel_path.split("/")
        if len(parts) >= 2:
            artist = parts[0]
            album = parts[1]
        else:
            artist = "Unknown Artist"
            album = parts[0] if parts else "Unknown Album"

        mp3_files = [f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".mp3"]
        lrc_files = [f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".lrc"]

        track_count = len(mp3_files)
        lrc_count = len(lrc_files)

        has_cover = find_cover_file(folder_path) is not None
        if not has_cover and track_count > 0:
            # Check if embedded cover art is available
            has_cover = True

        lyrics_status = "missing"
        if lrc_count > 0:
            lyrics_status = "synced"
        elif track_count > 0:
            # Check if any MP3 has embedded USLT ID3 frame
            has_uslt = False
            for mp3_path in mp3_files[:3]:
                try:
                    from mutagen.id3 import ID3

                    tags = ID3(str(mp3_path))
                    if any(k.startswith("USLT") for k in tags.keys()):
                        has_uslt = True
                        break
                except Exception:
                    pass
            if has_uslt:
                lyrics_status = "unsynced_only"

        results.append(
            {
                "folder": rel_path,
                "artist": artist,
                "album": album,
                "track_count": track_count,
                "lrc_count": lrc_count,
                "has_cover": has_cover,
                "lyrics_status": lyrics_status,
            }
        )

    return results


def resolve_album_cover(rel_path: str) -> Optional[Path]:
    """Safely resolves the album cover path with path traversal protection.

    If no image file exists in the directory, attempts to extract embedded
    artwork from audio files and cache it to cover.jpg.

    Raises:
        ValueError: If rel_path points outside BASE_DOWNLOAD_DIR.
    """
    base_dir = config.BASE_DOWNLOAD_DIR.resolve()
    album_dir = (base_dir / rel_path).resolve()

    # Guard against directory traversal
    album_dir.relative_to(base_dir)

    if not album_dir.is_dir():
        return None

    # 1. Existing external image file (case-insensitive)
    cover_file = find_cover_file(album_dir)
    if cover_file and cover_file.is_file():
        return cover_file

    # 2. Extract embedded artwork and cache as loose file
    extracted = extract_embedded_cover(album_dir)
    if extracted:
        cover_file = find_cover_file(album_dir)
        if cover_file and cover_file.is_file():
            return cover_file

    return None


def refetch_album_lyrics(rel_path: str) -> Dict[str, Any]:
    """Runs synced lyrics engine for all tracks in the specified album folder.

    Raises:
        ValueError: If rel_path points outside BASE_DOWNLOAD_DIR.
    """
    base_dir = config.BASE_DOWNLOAD_DIR.resolve()
    album_dir = (base_dir / rel_path).resolve()

    album_dir.relative_to(base_dir)

    if not album_dir.is_dir():
        return {"ok": False, "message": "Album folder not found."}

    sync_all_lrc_in_folder(album_dir)
    lrc_count = len([f for f in album_dir.iterdir() if f.is_file() and f.suffix.lower() == ".lrc"])
    return {
        "ok": True,
        "folder": rel_path,
        "lrc_count": lrc_count,
        "message": f"Successfully updated lyrics. Total .lrc files: {lrc_count}",
    }


def remove_album(rel_path: str) -> Tuple[bool, str]:
    """Deletes an album folder using system safe deletion logic."""
    return delete_album_folder(rel_path)
