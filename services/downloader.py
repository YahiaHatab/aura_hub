"""Audio download pipelines for Aura Hub using yt-dlp and spotdl."""

import json
import logging
import os
import re
import shutil
import subprocess
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import config
from services.lyrics import sync_all_lrc_in_folder
from services.tagger import tag_album_hybrid, tag_playlist_hybrid
from utils.helpers import parse_genius_input, sanitize_filename

logger = logging.getLogger(__name__)

# Dedicated thread pool executor for CPU and subprocess-bound tasks
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="AuraDownloader")


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


def run_pipeline(
    media_url: str,
    genius_raw: str = "",
    status_updater: Optional[Callable[[str], None]] = None,
) -> Tuple[Path, Dict[str, Any]]:
    """Primary download and tagging orchestration pipeline for Spotify and YouTube sources."""
    is_spotify = "spotify.com" in media_url.lower()
    parsed_genius = parse_genius_input(genius_raw)

    if status_updater:
        status_updater("📥 `[1/4]` *Streaming & Extracting Audio Files...*")

    if is_spotify:
        spotdl_bin = find_spotdl_binary()
        if not spotdl_bin:
            raise RuntimeError(
                "spotdl binary was not found. Please install spotdl (`pip install spotdl`) "
                "or place it in PATH."
            )

        clean_url = media_url.split("?")[0].strip()
        url_path = urllib.parse.urlsplit(clean_url).path.strip("/").split("/")
        category = url_path[0] if url_path else "spotify"
        slug = url_path[1] if len(url_path) > 1 else "collection"
        folder_label = f"Spotify_{category.title()}_{slug[:8]}"

        target_folder = config.BASE_DOWNLOAD_DIR / "Spotify Downloads" / folder_label
        target_folder.mkdir(parents=True, exist_ok=True)

        cmd = [
            spotdl_bin,
            "download",
            clean_url,
            "--output",
            f"{target_folder}/{{artist}} - {{title}}.{{output-ext}}",
            "--format",
            "mp3",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"spotdl failed:\n{res.stderr or res.stdout}")

        if parsed_genius:
            meta = tag_album_hybrid(
                target_folder,
                folder_label,
                "Various",
                genius_raw,
                parsed_genius,
                status_updater,
            )
        else:
            meta = tag_playlist_hybrid(target_folder, status_updater)

        if status_updater:
            status_updater("🎤 `[4/4]` *Fetching Synced .lrc Lyrics...*")
        sync_all_lrc_in_folder(target_folder)
        return target_folder, meta

    else:
        if not shutil.which("yt-dlp"):
            raise RuntimeError("yt-dlp binary is not installed or not in PATH.")

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

        clean_album = re.sub(r"^(Album|Playlist)\s*[-:]\s*", "", raw_album, flags=re.IGNORECASE).strip()
        clean_album = sanitize_filename(clean_album)
        clean_artist = sanitize_filename(raw_artist)

        if (
            clean_artist.lower() in ("unknown artist", "youtube", "")
            and parsed_genius
            and "artist" in parsed_genius
        ):
            clean_artist = sanitize_filename(parsed_genius["artist"])

        target_folder = config.BASE_DOWNLOAD_DIR / clean_artist / clean_album
        target_folder.mkdir(parents=True, exist_ok=True)

        output_tmpl = str(target_folder / "%(track_number,playlist_index)02d - %(title)s.%(ext)s")
        cmd = [
            "yt-dlp",
            "-x",
            "--audio-format",
            "mp3",
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
            raise RuntimeError(f"yt-dlp failed:\n{res.stderr or res.stdout}")

        meta = tag_album_hybrid(
            target_folder,
            clean_album,
            clean_artist,
            genius_raw,
            parsed_genius,
            status_updater,
        )

        if status_updater:
            status_updater("🎤 `[4/4]` *Fetching Synced .lrc Lyrics...*")
        sync_all_lrc_in_folder(target_folder)
        return target_folder, meta


def run_retag_folder(
    target_folder: Union[str, Path],
    genius_raw: str = "",
    status_updater: Optional[Callable[[str], None]] = None,
) -> Tuple[Path, Dict[str, Any]]:
    """Retags an existing local music directory and downloads missing synced lyrics."""
    folder_path = Path(target_folder).resolve()
    folder_name = folder_path.name
    parent_name = folder_path.parent.name
    parsed = parse_genius_input(genius_raw)

    album_name = parsed["album"] if (parsed and "album" in parsed) else folder_name
    artist_name = parsed["artist"] if (parsed and "artist" in parsed) else parent_name

    meta = tag_album_hybrid(
        folder_path, album_name, artist_name, genius_raw, parsed, status_updater
    )

    if status_updater:
        status_updater("🎤 `[4/4]` *Generating Synced .lrc Lyrics...*")
    sync_all_lrc_in_folder(folder_path)

    return folder_path, meta
