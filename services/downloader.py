"""Audio download pipelines for Aura Hub using yt-dlp and spotdl.

Implements multi-tier download dispatching across streaming services and YouTube,
supporting qualities: 'best' (optimal native Opus/AAC container) and 'mp3' (standard 320k).
"""

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import mutagen

import config
from services.lyrics import sync_all_lrc_in_folder
from services.system import rehome_album_folder
from services.tagger import tag_album_hybrid, tag_playlist_hybrid
from utils.helpers import parse_genius_input, resolve_fallback_genre, sanitize_filename

logger = logging.getLogger(__name__)

# Dedicated thread pool executor for CPU and subprocess-bound tasks
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="AuraDownloader")

LOSSLESS_DOMAINS = ("spotify.com", "deezer.com", "tidal.com", "qobuz.com")


def find_spotdl_binary() -> Optional[str]:
    """Locates the spotdl executable across standard PATH and local user bin directories."""
    spotdl_path = shutil.which("spotdl")
    if spotdl_path:
        return spotdl_path

    # Fallback to common Linux user installation paths
    local_bin = Path.home() / ".local" / "bin" / "spotdl"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin)

    return None


def run_youtube_search(query: str, limit: int = 5) -> List[Dict[str, str]]:
    """Searches YouTube using yt-dlp flat-playlist extraction and returns candidate tracks."""
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp executable is not installed or not in PATH.")

    cmd = [
        "yt-dlp",
        f"ytsearch{limit}:{query}",
        "--dump-single-json",
        "--flat-playlist",
        "--default-search",
        "ytsearch",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    entries = data.get("entries", [])
    results: List[Dict[str, str]] = []

    for entry in entries:
        track_id = entry.get("id", "")
        results.append(
            {
                "id": track_id,
                "title": entry.get("title", "Unknown Title"),
                "uploader": entry.get("uploader", "Unknown Artist"),
                "url": entry.get("url") or f"https://www.youtube.com/watch?v={track_id}",
            }
        )
    return results


def download_media_staging(
    media_url: str,
    genius_raw: str = "",
    status_updater: Optional[Callable[[str], None]] = None,
    quality: str = "auto",
) -> Dict[str, Any]:
    """Downloads audio files into an isolated staging directory prior to metadata review.

    Probes media titles and audio durations without applying tags or moving to active library.
    """
    quality = (quality or "best").lower().strip()
    if quality not in ("best", "auto", "opus", "mp3"):
        quality = "best"

    is_lossless_source = any(d in media_url.lower() for d in LOSSLESS_DOMAINS)
    parsed_genius = parse_genius_input(genius_raw)

    staging_id = secrets.token_hex(6)
    staging_dir = config.BASE_DOWNLOAD_DIR / ".staging" / f"dl_{staging_id}"
    staging_dir.mkdir(parents=True, exist_ok=True)

    if status_updater:
        status_updater("📥 `[1/4]` *Streaming & Extracting Audio Files...*")

    detected_artist = "Unknown Artist"
    detected_album = "Unknown Album"

    # ================= CASE 2: STREAMING SERVICES =================
    if is_lossless_source:
        clean_url = media_url.split("?")[0].strip()
        url_path = urllib.parse.urlsplit(clean_url).path.strip("/").split("/")
        category = url_path[0] if url_path else "media"
        slug = url_path[1] if len(url_path) > 1 else "collection"

        domain_name = "Streaming"
        if "spotify.com" in clean_url.lower():
            domain_name = "Spotify"
        elif "tidal.com" in clean_url.lower():
            domain_name = "Tidal"
        elif "deezer.com" in clean_url.lower():
            domain_name = "Deezer"
        elif "qobuz.com" in clean_url.lower():
            domain_name = "Qobuz"

        folder_label = f"{domain_name}_{category.title()}_{slug[:8]}"
        audio_format = "mp3" if quality == "mp3" else "opus"

        if domain_name == "Spotify":
            spotdl_bin = find_spotdl_binary()
            if spotdl_bin:
                cmd = [
                    spotdl_bin,
                    "download",
                    clean_url,
                    "--output",
                    f"{staging_dir}/{{artist}} - {{title}}.{{output-ext}}",
                    "--format",
                    audio_format,
                ]
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode != 0:
                    shutil.rmtree(staging_dir, ignore_errors=True)
                    raise RuntimeError(f"spotdl failed:\n{res.stderr or res.stdout}")
            else:
                if not shutil.which("yt-dlp"):
                    shutil.rmtree(staging_dir, ignore_errors=True)
                    raise RuntimeError(
                        "spotdl binary was not found and yt-dlp is unavailable. "
                        "Please install spotdl (`pip install spotdl`) or yt-dlp."
                    )
                cmd = [
                    "yt-dlp",
                    "-x",
                    "--audio-format",
                    audio_format,
                    "--audio-quality",
                    "0",
                    "--embed-thumbnail",
                    "--embed-metadata",
                    "-o",
                    str(staging_dir / "%(title)s.%(ext)s"),
                    clean_url,
                ]
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode != 0:
                    shutil.rmtree(staging_dir, ignore_errors=True)
                    raise RuntimeError(f"yt-dlp fallback failed:\n{res.stderr or res.stdout}")
        else:
            if not shutil.which("yt-dlp"):
                shutil.rmtree(staging_dir, ignore_errors=True)
                raise RuntimeError("yt-dlp binary is not installed or not in PATH.")
            cmd = [
                "yt-dlp",
                "-x",
                "--audio-format",
                audio_format,
                "--audio-quality",
                "0",
                "--embed-thumbnail",
                "--embed-metadata",
                "-o",
                str(staging_dir / "%(title)s.%(ext)s"),
                clean_url,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                shutil.rmtree(staging_dir, ignore_errors=True)
                raise RuntimeError(f"yt-dlp extraction failed:\n{res.stderr or res.stdout}")

        # Probed metadata for lossless streaming
        if parsed_genius:
            detected_album = parsed_genius.get("album") or folder_label
            detected_artist = parsed_genius.get("artist") or domain_name
        else:
            audio_files_tmp = [
                f for f in staging_dir.iterdir()
                if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS
            ]
            if audio_files_tmp:
                try:
                    mut = mutagen.File(str(audio_files_tmp[0]))
                    if mut:
                        cand_art = getattr(mut, "tags", None)
                        if cand_art:
                            detected_artist = (
                                str(cand_art.get("artist", [""])[0])
                                or str(cand_art.get("TPE1", [""])[0])
                                or domain_name
                            )
                            detected_album = (
                                str(cand_art.get("album", [""])[0])
                                or str(cand_art.get("TALB", [""])[0])
                                or folder_label
                            )
                except Exception:
                    pass
            if not detected_artist or detected_artist == "Unknown Artist":
                detected_artist = domain_name
            if not detected_album or detected_album == "Unknown Album":
                detected_album = folder_label

    # ================= CASE 1: YOUTUBE & YOUTUBE MUSIC =================
    else:
        if not shutil.which("yt-dlp"):
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise RuntimeError("yt-dlp binary is not installed or not in PATH.")

        audio_format = "mp3" if quality == "mp3" else "opus"

        probe_cmd = ["yt-dlp", "--dump-single-json", "--flat-playlist", media_url]
        probe_res = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        album_meta = json.loads(probe_res.stdout)

        raw_album = album_meta.get("album") or album_meta.get("title") or "Unknown Album"
        raw_artist = album_meta.get("artist") or album_meta.get("uploader") or ""

        entries = album_meta.get("entries", [])
        if (not raw_artist or raw_artist.lower() in ("unknown artist", "youtube")) and entries:
            first_entry = entries[0]
            raw_artist = (
                first_entry.get("artist")
                or first_entry.get("creator")
                or first_entry.get("uploader", "").replace(" - Topic", "").strip()
                or ""
            )
            if not raw_artist and " - " in first_entry.get("title", ""):
                raw_artist = first_entry.get("title").split(" - ")[0].strip()

        if not raw_artist:
            raw_artist = "Unknown Artist"

        clean_album = re.sub(
            r"^(Album|Playlist)\s*[-:]\s*", "", raw_album, flags=re.IGNORECASE
        ).strip()
        clean_album = sanitize_filename(clean_album)
        clean_artist = sanitize_filename(raw_artist)

        if (
            clean_artist.lower() in ("unknown artist", "youtube", "")
            and parsed_genius
            and "artist" in parsed_genius
        ):
            clean_artist = sanitize_filename(parsed_genius["artist"])

        detected_artist = clean_artist
        detected_album = clean_album

        output_tmpl = str(
            staging_dir / "%(track_number,playlist_index)02d - %(title)s.%(ext)s"
        )
        cmd = [
            "yt-dlp",
            "-x",
            "--audio-format",
            audio_format,
            "--audio-quality",
            "0",
            "--embed-thumbnail",
            "--embed-metadata",
            "-o",
            output_tmpl,
            media_url,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise RuntimeError(f"yt-dlp failed:\n{res.stderr or res.stdout}")

    # Inspect downloaded audio files
    audio_files = sorted([
        f for f in staging_dir.iterdir()
        if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS
    ])

    durations: List[float] = []
    for af in audio_files:
        try:
            mut = mutagen.File(str(af))
            if mut and getattr(mut, "info", None) and hasattr(mut.info, "length"):
                durations.append(float(mut.info.length))
        except Exception:
            pass

    return {
        "session_id": staging_id,
        "staging_dir": staging_dir,
        "detected_artist": detected_artist,
        "detected_album": detected_album,
        "audio_files": audio_files,
        "durations": durations,
        "genius_raw": genius_raw,
        "parsed_genius": parsed_genius,
        "quality": quality,
        "is_single": len(audio_files) == 1,
    }


def finalize_staged_media(
    staging_dir: Union[str, Path],
    detected_artist: str = "",
    detected_album: str = "",
    chosen_metadata: Optional[Any] = None,
    skip_tagging: bool = False,
    genius_raw: str = "",
    parsed_genius: Optional[Dict[str, str]] = None,
    status_updater: Optional[Callable[[str], None]] = None,
) -> Tuple[Path, Dict[str, Any]]:
    """Applies metadata & lyrics to staged audio files, then moves them to active library."""
    staging_path = Path(staging_dir).resolve()
    if not staging_path.exists():
        raise FileNotFoundError(f"Staging directory `{staging_path}` does not exist.")

    if skip_tagging:
        meta = {
            "album": detected_album or "Album",
            "artist": detected_artist or "Artist",
            "genre": resolve_fallback_genre(detected_artist, detected_album),
        }
    else:
        if status_updater:
            status_updater("⚡ `[3/4]` *Applying metadata & tags...*")
        meta = tag_album_hybrid(
            staging_path,
            detected_album,
            detected_artist,
            genius_raw,
            parsed_genius,
            status_updater,
            chosen_metadata=chosen_metadata,
        )
        if status_updater:
            status_updater("🎤 `[4/4]` *Fetching Synced .lrc Lyrics...*")
        sync_all_lrc_in_folder(staging_path)

    effective_artist = meta.get("artist") or detected_artist or "Unknown Artist"
    effective_album = meta.get("album") or detected_album or "Unknown Album"
    clean_artist = sanitize_filename(effective_artist)
    clean_album = sanitize_filename(effective_album)

    final_dir = config.BASE_DOWNLOAD_DIR / clean_artist / clean_album
    final_dir.mkdir(parents=True, exist_ok=True)

    # Move files into final Navidrome library directory
    for f in list(staging_path.iterdir()):
        if f.is_file():
            dest_f = final_dir / f.name
            if dest_f.exists():
                try:
                    dest_f.unlink()
                except Exception:
                    pass
            shutil.move(str(f), str(dest_f))

    # Clean empty staging directory and .staging parent if empty
    try:
        shutil.rmtree(str(staging_path), ignore_errors=True)
        staging_parent = config.BASE_DOWNLOAD_DIR / ".staging"
        if staging_parent.exists() and not any(staging_parent.iterdir()):
            staging_parent.rmdir()
    except Exception:
        pass

    if effective_artist and effective_artist.lower() not in (
        "various",
        "unknown artist",
        "various artists",
    ):
        final_dir = rehome_album_folder(final_dir, effective_artist)

    return final_dir, meta


def run_pipeline(
    media_url: str,
    genius_raw: str = "",
    status_updater: Optional[Callable[[str], None]] = None,
    quality: str = "auto",
) -> Tuple[Path, Dict[str, Any]]:
    """Primary download and tagging orchestration pipeline.

    Chains download_media_staging and finalize_staged_media with default hybrid tagging.
    """
    stage = download_media_staging(media_url, genius_raw, status_updater, quality)
    return finalize_staged_media(
        staging_dir=stage["staging_dir"],
        detected_artist=stage["detected_artist"],
        detected_album=stage["detected_album"],
        chosen_metadata=None,
        genius_raw=genius_raw,
        parsed_genius=stage.get("parsed_genius"),
        status_updater=status_updater,
    )


def run_retag_folder(
    target_folder: Union[str, Path],
    genius_raw: str = "",
    status_updater: Optional[Callable[[str], None]] = None,
    chosen_metadata: Optional[Any] = None,
) -> Tuple[Path, Dict[str, Any]]:
    """Retags an existing local music directory, unifies artist folder, and downloads missing synced lyrics."""
    import mutagen
    folder_path = Path(target_folder).resolve()
    folder_name = folder_path.name
    parent_name = folder_path.parent.name
    parsed = parse_genius_input(genius_raw)

    album_name = parsed["album"] if (parsed and "album" in parsed) else folder_name
    artist_name = parsed["artist"] if (parsed and "artist" in parsed) else parent_name

    if chosen_metadata is not None:
        meta = tag_album_hybrid(
            folder_path,
            album_name,
            artist_name,
            genius_raw,
            parsed,
            status_updater,
            chosen_metadata=chosen_metadata,
        )
    else:
        # Inspect local folder tracks and durations to feed recommendation engine
        audio_files = sorted([
            f for f in folder_path.iterdir()
            if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS
        ])
        local_durations: List[float] = []
        for af in audio_files:
            try:
                mut = mutagen.File(str(af))
                if mut and getattr(mut, "info", None) and hasattr(mut.info, "length"):
                    local_durations.append(float(mut.info.length))
            except Exception:
                pass

        if status_updater:
            status_updater("🔎 `[1/4]` *Querying metadata providers (iTunes, MusicBrainz, Deezer)...*")

        from services.metadata import search_album_metadata_candidates

        candidates = search_album_metadata_candidates(
            album_name,
            artist_name,
            local_track_count=len(audio_files),
            local_durations=local_durations,
        )

        selected_cand = None
        if candidates and candidates[0].confidence_score >= 40.0:
            selected_cand = candidates[0].album_data

        meta = tag_album_hybrid(
            folder_path,
            album_name,
            artist_name,
            genius_raw,
            parsed,
            status_updater,
            chosen_metadata=selected_cand,
        )

    # Re-home folder if canonical artist differs from directory
    effective_artist = meta.get("artist") or ""
    if effective_artist:
        folder_path = rehome_album_folder(folder_path, effective_artist)

    if status_updater:
        status_updater("🎤 `[4/4]` *Generating Synced .lrc Lyrics...*")
    sync_all_lrc_in_folder(folder_path)
    return folder_path, meta
