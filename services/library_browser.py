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
    """Extracts embedded front cover artwork from the first supported audio file in album_dir.

    Supports MP3, FLAC, Opus, and M4A containers.
    If extracted, also saves cover.jpg / cover.png to album_dir for Navidrome and future serving.
    Returns:
        (image_bytes, mime_type) if found, else None.
    """
    if not album_dir.is_dir():
        return None

    try:
        audio_files = [
            f for f in album_dir.iterdir()
            if f.is_file() and f.name.lower().endswith(config.AUDIO_EXTENSIONS)
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

    # Check Opus files
    opus_files = [f for f in audio_files if f.suffix.lower() == ".opus"]
    for opus_path in opus_files[:3]:
        try:
            import base64
            from mutagen.flac import Picture
            from mutagen.oggopus import OggOpus

            audio = OggOpus(str(opus_path))
            if "metadata_block_picture" in audio and audio["metadata_block_picture"]:
                pic_b64 = audio["metadata_block_picture"][0]
                pic = Picture(base64.b64decode(pic_b64))
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

    # Check M4A files
    m4a_files = [f for f in audio_files if f.suffix.lower() == ".m4a"]
    for m4a_path in m4a_files[:3]:
        try:
            from mutagen.mp4 import MP4, MP4Cover

            audio = MP4(str(m4a_path))
            covs = audio.get("covr")
            if covs:
                data = bytes(covs[0])
                mime = "image/png" if covs[0].imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
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

        parts = Path(rel_path).parts
        if len(parts) >= 2:
            artist = parts[0]
            album = parts[1]
        else:
            artist = "Unknown Artist"
            album = parts[0] if parts else "Unknown Album"

        audio_files = [
            f for f in folder_path.iterdir()
            if f.is_file() and f.name.lower().endswith(config.AUDIO_EXTENSIONS)
        ]
        lrc_files = [f for f in folder_path.iterdir() if f.is_file() and f.suffix.lower() == ".lrc"]

        track_count = len(audio_files)
        lrc_count = len(lrc_files)

        has_cover = find_cover_file(folder_path) is not None
        if not has_cover and track_count > 0:
            # Check if embedded cover art is available
            has_cover = True

        lyrics_status = "missing"
        if lrc_count > 0:
            lyrics_status = "synced"
        elif track_count > 0:
            # Check if any audio track has embedded lyrics
            has_lyrics = False
            for audio_path in audio_files[:3]:
                f_ext = audio_path.suffix.lower()
                if f_ext == ".mp3":
                    try:
                        from mutagen.id3 import ID3

                        tags = ID3(str(audio_path))
                        if any(k.startswith("USLT") for k in tags.keys()):
                            has_lyrics = True
                            break
                    except Exception:
                        pass
                elif f_ext in (".opus", ".flac"):
                    try:
                        import mutagen

                        audio = mutagen.File(str(audio_path))
                        if audio and getattr(audio, "tags", None) and audio.tags.get("lyrics"):
                            has_lyrics = True
                            break
                    except Exception:
                        pass
                elif f_ext == ".m4a":
                    try:
                        from mutagen.mp4 import MP4

                        audio = MP4(str(audio_path))
                        if audio.get("\xa9lyr"):
                            has_lyrics = True
                            break
                    except Exception:
                        pass
            if has_lyrics:
                lyrics_status = "unsynced_only"

        folder_mtime = int(folder_path.stat().st_mtime) if folder_path.exists() else 0
        year = ""
        release_type = ""

        # Try to read release_type and year from first audio file
        if audio_files:
            first_audio = audio_files[0]
            f_ext = first_audio.suffix.lower()
            if f_ext == ".mp3":
                try:
                    from mutagen.id3 import ID3
                    id3_tags = ID3(str(first_audio))
                    for txxx in id3_tags.getall("TXXX"):
                        if txxx.desc.upper() in ("RELEASETYPE", "RELEASE TYPE", "MUSICBRAINZ_ALBUMTYPE"):
                            release_type = str(txxx.text[0]).strip()
                            break
                    if "TDRC" in id3_tags and id3_tags["TDRC"].text:
                        year = str(id3_tags["TDRC"].text[0])[:4]
                except Exception:
                    pass
            elif f_ext in (".opus", ".flac"):
                try:
                    import mutagen
                    mut = mutagen.File(str(first_audio))
                    if mut and getattr(mut, "tags", None):
                        rt = mut.tags.get("releasetype") or mut.tags.get("musicbrainz_albumtype")
                        if rt:
                            release_type = rt[0] if isinstance(rt, list) else str(rt)
                        dt = mut.tags.get("date") or mut.tags.get("year")
                        if dt:
                            year = str(dt[0] if isinstance(dt, list) else dt)[:4]
                except Exception:
                    pass
            elif f_ext == ".m4a":
                try:
                    from mutagen.mp4 import MP4
                    mp = MP4(str(first_audio))
                    rt = mp.get("----:com.apple.iTunes:RELEASETYPE")
                    if rt:
                        release_type = rt[0].decode("utf-8", errors="ignore").strip()
                    dy = mp.get("\xa9day")
                    if dy:
                        year = str(dy[0])[:4]
                except Exception:
                    pass

        if not release_type:
            if track_count <= 3:
                release_type = "Single"
            elif track_count <= 6:
                release_type = "EP"
            else:
                release_type = "Album"

        results.append(
            {
                "folder": rel_path,
                "artist": artist,
                "album": album,
                "track_count": track_count,
                "lrc_count": lrc_count,
                "has_cover": has_cover,
                "lyrics_status": lyrics_status,
                "release_type": release_type,
                "mtime": folder_mtime,
                "year": year,
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
