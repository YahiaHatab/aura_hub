"""Metadata resolution service for Aura Hub.

Integrates MusicBrainz REST API, Cover Art Archive, Chromaprint/AcoustID audio fingerprinting,
and Genius API for composers, producers, and unsynced lyrics.
"""

import json
import logging
import os
import shutil
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import acoustid
import lyricsgenius

import config
from utils.helpers import extract_clean_artists, franco_to_arabic, get_clean_name

logger = logging.getLogger(__name__)


# ---------------- ACOUSTID AUDIO FINGERPRINTING ----------------
def get_acoustid_metadata(file_path: str) -> Optional[Dict[str, str]]:
    """Calculates Chromaprint audio fingerprint using fpcalc and resolves MusicBrainz release ID."""
    try:
        if not shutil.which("fpcalc"):
            logger.info("Chromaprint binary 'fpcalc' not found in PATH; skipping AcoustID fingerprinting.")
            return None

        if not os.path.exists(file_path):
            return None

        results = acoustid.match(config.ACOUSTID_API_KEY, file_path, parse=False)
        for res in results.get("results", []):
            recordings = res.get("recordings", [])
            if recordings:
                rec_id = recordings[0].get("id")
                lookup_url = (
                    f"https://musicbrainz.org/ws/2/recording/{rec_id}?"
                    f"inc=releases+artist-credits&fmt=json"
                )
                req = urllib.request.Request(lookup_url, headers=config.MB_HEADERS)
                with urllib.request.urlopen(req, timeout=8) as r:
                    rec_data = json.loads(r.read().decode("utf-8"))

                releases = rec_data.get("releases", [])
                if releases:
                    artist_credit = ""
                    credits = rec_data.get("artist-credit", [])
                    if credits:
                        artist_credit = credits[0].get("name") or credits[0].get(
                            "artist", {}
                        ).get("name", "")
                    return {"release_mbid": releases[0]["id"], "artist": artist_credit}
    except Exception as e:
        logger.warning(f"AcoustID lookup failed for {file_path}: {e}")
    return None


# ---------------- COVER ART ARCHIVE ----------------
def fetch_cover_art_archive(release_mbid: str) -> Optional[bytes]:
    """Fetches high-resolution album artwork from Cover Art Archive."""
    endpoints = [
        f"https://coverartarchive.org/release/{release_mbid}/front-500",
        f"https://coverartarchive.org/release/{release_mbid}/front",
    ]
    for url in endpoints:
        try:
            req = urllib.request.Request(url, headers=config.MB_HEADERS)
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = resp.read()
                if data:
                    return data
        except Exception:
            continue
    return None


# ---------------- MUSICBRAINZ REST CLIENT ----------------
def fetch_full_mb_release(
    rel_id: str, fallback_title: str, fallback_artist: str
) -> Optional[Dict[str, Any]]:
    """Retrieves release details, tracklist, genre tags, and cover art from MusicBrainz."""
    try:
        lookup_url = (
            f"https://musicbrainz.org/ws/2/release/{rel_id}?"
            f"inc=recordings+genres+tags+artist-credits&fmt=json"
        )
        req_details = urllib.request.Request(lookup_url, headers=config.MB_HEADERS)
        with urllib.request.urlopen(req_details, timeout=8) as resp:
            full_data = json.loads(resp.read().decode("utf-8"))

        tags = full_data.get("genres", []) or full_data.get("tags", [])
        genre_str = ""
        if tags:
            tags.sort(key=lambda x: x.get("count", 0), reverse=True)
            valid = [
                t["name"].title()
                for t in tags
                if t.get("name", "").lower() not in ("music", "all")
            ]
            genre_str = ", ".join(valid[:2])

        tracks: List[Dict[str, Any]] = []
        for medium in full_data.get("media", []):
            for trk in medium.get("tracks", []):
                tracks.append(
                    {
                        "position": trk.get("position"),
                        "title": trk.get("title", ""),
                        "length": float(trk.get("length") or 0) / 1000.0,
                        "recording_id": trk.get("recording", {}).get("id"),
                    }
                )

        cover_data = fetch_cover_art_archive(rel_id)

        artist_credit = ""
        credits = full_data.get("artist-credit", [])
        if credits:
            artist_credit = credits[0].get("name") or credits[0].get(
                "artist", {}
            ).get("name", "")

        return {
            "mbid": rel_id,
            "title": full_data.get("title", fallback_title),
            "artist": artist_credit or fallback_artist,
            "date": full_data.get("date", ""),
            "genre": genre_str,
            "tracks": tracks,
            "cover_bytes": cover_data,
        }
    except Exception as e:
        logger.warning(f"Error fetching full MusicBrainz release {rel_id}: {e}")
        return None


def search_musicbrainz_release(
    album_name: str, artist_name: str = "", sample_file: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Multi-stage release search using Clean Artist Candidates, Franco-Arabic transliterations, and AcoustID."""
    candidates: List[Dict[str, Any]] = []
    artist_candidates = extract_clean_artists(artist_name)
    album_variants = franco_to_arabic(album_name)

    # Stage 1: Exact search across bilingual artist candidates & album transliterations
    if artist_candidates:
        for art in artist_candidates:
            for alb in album_variants:
                try:
                    q = f'release:"{alb}" AND artist:"{art}"'
                    params = urllib.parse.urlencode({"query": q, "fmt": "json", "limit": "5"})
                    url = f"https://musicbrainz.org/ws/2/release?{params}"
                    req = urllib.request.Request(url, headers=config.MB_HEADERS)
                    with urllib.request.urlopen(req, timeout=6) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    rels = data.get("releases", [])
                    if rels:
                        candidates = rels
                        break
                except Exception:
                    continue
            if candidates:
                break

    # Stage 2: Audio fingerprinting using Chromaprint/AcoustID if text search returned nothing
    if not candidates and sample_file and os.path.exists(sample_file):
        logger.info(f"Fingerprinting {sample_file} via AcoustID...")
        acoust_meta = get_acoustid_metadata(sample_file)
        if acoust_meta:
            return fetch_full_mb_release(
                acoust_meta["release_mbid"], album_name, acoust_meta["artist"]
            )

    # Stage 3: Relaxed release title search without artist constraint
    if not candidates and not artist_candidates:
        for alb in album_variants:
            try:
                q = f'release:"{alb}"'
                params = urllib.parse.urlencode({"query": q, "fmt": "json", "limit": "5"})
                url = f"https://musicbrainz.org/ws/2/release?{params}"
                req = urllib.request.Request(url, headers=config.MB_HEADERS)
                with urllib.request.urlopen(req, timeout=6) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                rels = data.get("releases", [])
                if rels:
                    candidates = rels
                    break
            except Exception:
                continue

    if not candidates:
        return None

    # Prioritize releases with front cover artwork in Cover Art Archive
    best_rel = None
    for rel in candidates:
        if rel.get("cover-art-archive", {}).get("front"):
            best_rel = rel
            break
    if not best_rel:
        best_rel = candidates[0]

    chosen_artist = artist_candidates[0] if artist_candidates else artist_name
    return fetch_full_mb_release(best_rel["id"], album_name, chosen_artist)


def search_musicbrainz_track(title: str, artist: str = "") -> Dict[str, str]:
    """Searches for an individual track on MusicBrainz by title and artist."""
    try:
        artist_candidates = extract_clean_artists(artist)
        primary_artist = artist_candidates[0] if artist_candidates else ""
        variants = franco_to_arabic(title)

        for q_title in variants:
            query = (
                f'recording:"{q_title}" AND artist:"{primary_artist}"'
                if primary_artist
                else f'recording:"{q_title}"'
            )
            params = urllib.parse.urlencode({"query": query, "fmt": "json", "limit": "3"})
            url = f"https://musicbrainz.org/ws/2/recording?{params}"
            req = urllib.request.Request(url, headers=config.MB_HEADERS)
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            recordings = data.get("recordings", [])
            if recordings:
                rec = recordings[0]
                tags = rec.get("tags", [])
                genre_str = ""
                if tags:
                    tags.sort(key=lambda x: x.get("count", 0), reverse=True)
                    valid = [
                        t["name"].title()
                        for t in tags
                        if t.get("name", "").lower() not in ("music", "all")
                    ]
                    genre_str = ", ".join(valid[:2])

                artist_credit = ""
                credits = rec.get("artist-credit", [])
                if credits:
                    artist_credit = credits[0].get("name", "")

                return {
                    "title": rec.get("title", title),
                    "artist": artist_credit or primary_artist or artist,
                    "genre": genre_str,
                }
    except Exception as e:
        logger.warning(f"MusicBrainz track lookup failed for '{title}': {e}")
    return {}


# ---------------- GENIUS CLIENT & SEARCH ----------------
def get_genius_client() -> Optional[lyricsgenius.Genius]:
    """Initializes and returns a Genius API client instance."""
    if not config.GENIUS_ACCESS_TOKEN:
        return None
    try:
        genius = lyricsgenius.Genius(
            config.GENIUS_ACCESS_TOKEN,
            timeout=12,
            retries=1,
            verbose=False,
        )
        genius.remove_section_headers = False
        return genius
    except Exception as e:
        logger.warning(f"Failed to initialize Genius client: {e}")
        return None


def search_genius_album(
    album_name: str,
    artist_name: str = "",
    parsed_genius: Optional[Dict[str, Any]] = None,
    genius_raw: str = "",
) -> Any:
    """Searches for an album on Genius using parsed tokens or fuzzy title/artist matching."""
    genius = get_genius_client()
    if not genius:
        return None

    try:
        if parsed_genius and "album_id" in parsed_genius:
            return genius.search_album(album_id=parsed_genius["album_id"])
        elif parsed_genius and "album" in parsed_genius:
            return genius.search_album(
                parsed_genius["album"], parsed_genius["artist"]
            )
        elif genius_raw:
            return genius.search_album(genius_raw)
        else:
            artist_cand = extract_clean_artists(artist_name)
            search_art = artist_cand[0] if artist_cand else None
            for v in franco_to_arabic(album_name):
                genius_album = genius.search_album(v, search_art)
                if genius_album:
                    return genius_album
    except Exception as e:
        logger.warning(f"Genius album search failed: {e}")
    return None
