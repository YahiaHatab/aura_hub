"""LRCLIB synced lyrics engine for Aura Hub.

Fetches synchronized karaoke lyrics (.lrc) from lrclib.net and writes them next to audio files.
"""

import json
import logging
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Union

from mutagen.id3 import ID3

import config
from utils.helpers import extract_clean_artists, franco_to_arabic

logger = logging.getLogger(__name__)


def fetch_and_save_lrc(
    track_title: str, artist_name: str, target_lrc_path: Union[str, Path]
) -> bool:
    """Queries LRCLIB (exact match then search fallback) for synced lyrics and writes .lrc file."""
    target_path = Path(target_lrc_path)
    artists_to_try = extract_clean_artists(artist_name) or [artist_name]
    title_variants = franco_to_arabic(track_title)

    queries = []
    for art in artists_to_try:
        for tit in title_variants:
            queries.append((tit, art))

    headers = config.MB_HEADERS

    for title_q, artist_q in queries:
        # 1. Exact match attempt
        try:
            params = urllib.parse.urlencode({"track_name": title_q, "artist_name": artist_q})
            req = urllib.request.Request(f"https://lrclib.net/api/get?{params}", headers=headers)
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                synced = data.get("syncedLyrics")
                if synced:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_text(synced, encoding="utf-8")
                    logger.info(f"Saved synced lyrics to {target_path.name} via exact match")
                    return True
        except Exception:
            pass

        # 2. General search fallback attempt
        try:
            search_q = urllib.parse.urlencode({"q": f"{artist_q} {title_q}"})
            req = urllib.request.Request(f"https://lrclib.net/api/search?{search_q}", headers=headers)
            with urllib.request.urlopen(req, timeout=6) as resp:
                results = json.loads(resp.read().decode("utf-8"))
                for item in results:
                    synced = item.get("syncedLyrics")
                    if synced:
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        target_path.write_text(synced, encoding="utf-8")
                        logger.info(f"Saved synced lyrics to {target_path.name} via search fallback")
                        return True
        except Exception:
            pass

    return False


def sync_all_lrc_in_folder(folder: Union[str, Path]) -> int:
    """Scans all audio files matching AUDIO_EXTENSIONS in a directory and fetches missing .lrc companion files.

    Returns the total number of synced .lrc files present in the folder.
    """
    folder_path = Path(folder)
    if not folder_path.is_dir():
        return 0

    audio_files = [
        f for f in folder_path.iterdir()
        if f.is_file() and f.name.lower().endswith(config.AUDIO_EXTENSIONS)
    ]

    for audio_path in audio_files:
        lrc_path = Path(os.path.splitext(str(audio_path))[0] + ".lrc")
        if lrc_path.exists():
            continue

        title, artist = "", ""
        f_ext = audio_path.suffix.lower()
        if f_ext == ".mp3":
            try:
                audio = ID3(str(audio_path))
                if "TIT2" in audio and audio["TIT2"].text:
                    title = str(audio["TIT2"].text[0])
                if "TPE1" in audio and audio["TPE1"].text:
                    artist = str(audio["TPE1"].text[0])
            except Exception:
                pass
        elif f_ext == ".opus":
            try:
                from mutagen.oggopus import OggOpus

                audio = OggOpus(str(audio_path))
                title = audio.get("title", [""])[0]
                artist = audio.get("artist", [""])[0]
            except Exception:
                pass
        elif f_ext == ".flac":
            try:
                from mutagen.flac import FLAC

                audio = FLAC(str(audio_path))
                title = audio.get("title", [""])[0]
                artist = audio.get("artist", [""])[0]
            except Exception:
                pass
        elif f_ext == ".m4a":
            try:
                from mutagen.mp4 import MP4

                audio = MP4(str(audio_path))
                title = audio.get("\xa9nam", [""])[0] if audio.get("\xa9nam") else ""
                artist = audio.get("\xa9ART", [""])[0] if audio.get("\xa9ART") else ""
            except Exception:
                pass

        if not title:
            stem = audio_path.stem
            if " - " in stem:
                parts = stem.split(" - ", 1)
                artist, title = parts[0].strip(), parts[1].strip()
            else:
                title = stem.strip()

        fetch_and_save_lrc(title, artist, lrc_path)

    total_lrcs = len([f for f in folder_path.iterdir() if f.suffix.lower() == ".lrc"])
    return total_lrcs

