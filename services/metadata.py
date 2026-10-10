"""Multi-source metadata aggregation and recommendation engine for Aura Hub.

Integrates:
- MusicBrainz REST API & Cover Art Archive (with strict rate-limiting)
- Deezer API (high-res artwork, tracklists, durations, contributors)
- iTunes Search API (worldwide catalogue, high-res jackets, durations)
- Spotify Web API (client credentials flow, albums, tracks, ISRCs)
- Discogs API (rich producer, composer, and arranger credits)
- Genius API (lyrics, composers, and producer credits)
- AcoustID & Chromaprint audio fingerprinting (via fpcalc)
- LRCLIB (synced and unsynced lyrics)

Implements fuzzy string matching, duration proximity scoring, track count alignment,
and a metadata completeness recommendation engine.
"""

import asyncio
import base64
import concurrent.futures
from dataclasses import asdict, dataclass, field
import difflib
import json
import logging
import os
from pathlib import Path
import shutil
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import acoustid
import httpx
import lyricsgenius

import config
from utils.helpers import (
    extract_clean_artists,
    franco_to_arabic,
    get_clean_name,
    is_arabic_music,
    resolve_fallback_genre,
)

logger = logging.getLogger(__name__)

# Cache for artist aliases from MusicBrainz
_ARTIST_ALIASES_CACHE: Dict[str, List[Dict[str, Any]]] = {}

# Rate limiter state for MusicBrainz
_LAST_MB_CALL_TIMESTAMP: float = 0.0
_MB_LOCK = asyncio.Lock() if hasattr(asyncio, "Lock") else None


# =====================================================================
# DATA MODELS & SCHEMAS
# =====================================================================

@dataclass
class UnifiedTrackMetadata:
    """Normalized metadata container for an individual audio track."""
    title: str = ""
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    track_number: int = 1
    total_tracks: int = 1
    disc_number: int = 1
    total_discs: int = 1
    year: str = ""
    genre: str = ""
    genres: List[str] = field(default_factory=list)
    producers: List[str] = field(default_factory=list)
    composers: List[str] = field(default_factory=list)
    arrangers: List[str] = field(default_factory=list)
    lyricists: List[str] = field(default_factory=list)
    lyrics_unsynced: str = ""
    lyrics_synced: str = ""
    duration_seconds: float = 0.0
    isrc: str = ""
    cover_url: str = ""
    cover_bytes: Optional[bytes] = None
    source: str = ""
    source_id: str = ""
    extra_tags: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("cover_bytes", None)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UnifiedTrackMetadata":
        fields = cls.__dataclass_fields__.keys()
        filtered = {k: v for k, v in data.items() if k in fields}
        return cls(**filtered)


@dataclass
class UnifiedAlbumMetadata:
    """Normalized metadata container for a full album or EP."""
    album: str = ""
    artist: str = ""
    album_artist: str = ""
    year: str = ""
    genre: str = ""
    genres: List[str] = field(default_factory=list)
    total_tracks: int = 0
    total_discs: int = 1
    cover_url: str = ""
    cover_bytes: Optional[bytes] = None
    tracks: List[UnifiedTrackMetadata] = field(default_factory=list)
    producers: List[str] = field(default_factory=list)
    composers: List[str] = field(default_factory=list)
    source: str = ""
    source_id: str = ""
    extra_tags: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("cover_bytes", None)
        d["tracks"] = [t.to_dict() if isinstance(t, UnifiedTrackMetadata) else t for t in self.tracks]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UnifiedAlbumMetadata":
        tracks_data = data.get("tracks", [])
        parsed_tracks = [
            UnifiedTrackMetadata.from_dict(t) if isinstance(t, dict) else t
            for t in tracks_data
        ]
        fields = cls.__dataclass_fields__.keys()
        filtered = {k: v for k, v in data.items() if k in fields and k != "tracks"}
        album = cls(**filtered)
        album.tracks = parsed_tracks
        return album


@dataclass
class MetadataCandidate:
    """Normalized candidate result returned by the recommendation engine."""
    source: str
    confidence_score: float
    is_recommended: bool
    album_data: Optional[UnifiedAlbumMetadata] = None
    track_data: Optional[UnifiedTrackMetadata] = None
    preview: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "confidence_score": round(self.confidence_score, 1),
            "is_recommended": self.is_recommended,
            "preview": self.preview,
            "album_data": self.album_data.to_dict() if self.album_data else None,
            "track_data": self.track_data.to_dict() if self.track_data else None,
        }


# =====================================================================
# STRING SIMILARITY & FUZZY MATCHING HELPERS
# =====================================================================

def calculate_string_similarity(a: str, b: str) -> float:
    """Calculates fuzzy similarity between two strings (0.0 to 1.0).

    Uses a blend of normalized Levenshtein sequence matching, exact substring containment,
    and token set Jaccard similarity.
    """
    clean_a = get_clean_name(a or "").strip().lower()
    clean_b = get_clean_name(b or "").strip().lower()
    if not clean_a or not clean_b:
        return 0.0
    if clean_a == clean_b:
        return 1.0

    # Substring containment bonus
    if clean_a in clean_b or clean_b in clean_a:
        len_ratio = min(len(clean_a), len(clean_b)) / max(len(clean_a), len(clean_b))
        containment_score = 0.85 + (0.15 * len_ratio)
    else:
        containment_score = 0.0

    tokens_a = set(w for w in clean_a.split() if len(w) > 1)
    tokens_b = set(w for w in clean_b.split() if len(w) > 1)
    if tokens_a and tokens_b:
        jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    else:
        jaccard = 0.0

    ratio = difflib.SequenceMatcher(None, clean_a, clean_b).ratio()
    return max(ratio, containment_score, jaccard)


def calculate_best_variant_similarity(query_variants: List[str], target_str: str) -> float:
    """Computes the maximum similarity across multiple query transliterations."""
    if not query_variants or not target_str:
        return 0.0
    return max(calculate_string_similarity(v, target_str) for v in query_variants)


# =====================================================================
# MUSICBRAINZ RATE LIMITER & BACKEND SERVICES
# =====================================================================

async def _throttle_musicbrainz() -> None:
    """Enforces polite rate limiting for MusicBrainz REST API calls."""
    global _LAST_MB_CALL_TIMESTAMP
    min_interval = getattr(config, "MB_RATE_LIMIT_DELAY", 1.0)
    now = time.monotonic()
    elapsed = now - _LAST_MB_CALL_TIMESTAMP
    if elapsed < min_interval:
        await asyncio.sleep(min_interval - elapsed)
    _LAST_MB_CALL_TIMESTAMP = time.monotonic()


def fetch_mb_artist_aliases(artist_mbid: str) -> List[Dict[str, Any]]:
    """Fetches artist entity aliases from MusicBrainz REST API if not already cached."""
    if not artist_mbid:
        return []
    if artist_mbid in _ARTIST_ALIASES_CACHE:
        return _ARTIST_ALIASES_CACHE[artist_mbid]

    try:
        url = f"https://musicbrainz.org/ws/2/artist/{artist_mbid}?inc=aliases&fmt=json"
        req = urllib.request.Request(url, headers=config.MB_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        aliases = data.get("aliases", [])
        _ARTIST_ALIASES_CACHE[artist_mbid] = aliases
        return aliases
    except Exception as e:
        logger.warning(f"Failed to fetch MusicBrainz aliases for artist MBID {artist_mbid}: {e}")
        return []


def resolve_canonical_artist(
    artist_credits: List[Dict[str, Any]],
    fallback_artist: str = "",
) -> str:
    """Resolves a Latin-Canonical artist name following a strict 3-tier cascade."""
    credit_name = ""
    artist_obj: Dict[str, Any] = {}
    if artist_credits:
        primary_credit = artist_credits[0]
        credit_name = (primary_credit.get("name") or "").strip()
        artist_obj = primary_credit.get("artist") or {}
        if not credit_name and artist_obj:
            credit_name = (artist_obj.get("name") or "").strip()

    aliases = list(artist_obj.get("aliases") or [])
    artist_mbid = artist_obj.get("id")
    if not aliases and artist_mbid:
        aliases = fetch_mb_artist_aliases(artist_mbid)

    # Tier 1: Official Latin/English Alias in MusicBrainz
    latin_candidates_tier1: List[Tuple[int, str]] = []
    for a in aliases:
        a_name = (a.get("name") or "").strip()
        if not a_name or not a_name.isascii():
            continue
        locale = (a.get("locale") or "").lower()
        a_type = a.get("type") or ""
        is_primary = bool(a.get("primary"))

        if locale.startswith("en") or a_type == "Artist name":
            score = 0
            if is_primary and locale.startswith("en"):
                score += 100
            elif is_primary and a_type == "Artist name":
                score += 90
            elif locale.startswith("en") and a_type == "Artist name":
                score += 80
            elif locale.startswith("en"):
                score += 70
            elif a_type == "Artist name":
                score += 60
            else:
                score += 50

            if "," not in a_name:
                score += 10

            latin_candidates_tier1.append((score, a_name))

    if latin_candidates_tier1:
        latin_candidates_tier1.sort(key=lambda x: x[0], reverse=True)
        return latin_candidates_tier1[0][1]

    # Tier 2: Source/Input Latin Candidate
    input_latin_candidates = [
        a for a in extract_clean_artists(fallback_artist) if a.isascii()
    ]
    if input_latin_candidates:
        return input_latin_candidates[0]

    if credit_name and credit_name.isascii():
        return credit_name

    artist_obj_name = (artist_obj.get("name") or "").strip()
    if artist_obj_name and artist_obj_name.isascii():
        return artist_obj_name

    # Tier 3: Native Script Fallback
    return credit_name or fallback_artist.strip()


# =====================================================================
# ACOUSTID AUDIO FINGERPRINTING
# =====================================================================

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
                    f"inc=releases+artist-credits+aliases&fmt=json"
                )
                req = urllib.request.Request(lookup_url, headers=config.MB_HEADERS)
                with urllib.request.urlopen(req, timeout=8) as r:
                    rec_data = json.loads(r.read().decode("utf-8"))

                releases = rec_data.get("releases", [])
                if releases:
                    credits = rec_data.get("artist-credit", [])
                    artist_credit = resolve_canonical_artist(credits)
                    return {"release_mbid": releases[0]["id"], "artist": artist_credit}
    except Exception as e:
        logger.warning(f"AcoustID lookup failed for {file_path}: {e}")
    return None


# =====================================================================
# COVER ART ARCHIVE
# =====================================================================

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


async def fetch_image_bytes_async(url: str) -> Optional[bytes]:
    """Asynchronously downloads image artwork bytes."""
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "AuraHub/1.0"})
            if resp.status_code == 200:
                return resp.content
    except Exception as e:
        logger.debug(f"Failed to fetch image bytes from {url}: {e}")
    return None


# =====================================================================
# MUSICBRAINZ REST CLIENT (EXPANDED TO RELATIONS & DEEP CREDITS)
# =====================================================================

def _extract_mb_relations(relations_list: List[Dict[str, Any]]) -> Tuple[List[str], List[str], List[str], List[str]]:
    """Extracts producers, composers, arrangers, and lyricists from MusicBrainz relationships."""
    producers: List[str] = []
    composers: List[str] = []
    arrangers: List[str] = []
    lyricists: List[str] = []

    for rel in relations_list:
        rel_type = (rel.get("type") or "").lower()
        art_name = rel.get("artist", {}).get("name", "")
        if not art_name:
            continue

        if "producer" in rel_type or "audio director" in rel_type:
            if art_name not in producers:
                producers.append(art_name)
        elif "composer" in rel_type:
            if art_name not in composers:
                composers.append(art_name)
        elif "arranger" in rel_type or "orchestrator" in rel_type:
            if art_name not in arrangers:
                arrangers.append(art_name)
        elif "lyricist" in rel_type or "writer" in rel_type:
            if art_name not in lyricists:
                lyricists.append(art_name)

    return producers, composers, arrangers, lyricists


def fetch_full_mb_release(
    rel_id: str, fallback_title: str, fallback_artist: str
) -> Optional[Dict[str, Any]]:
    """Retrieves release details, tracklist, genre tags, relations, and cover art from MusicBrainz."""
    try:
        lookup_url = (
            f"https://musicbrainz.org/ws/2/release/{rel_id}?"
            f"inc=recordings+genres+tags+artist-credits+aliases+artist-rels+work-rels+recording-rels&fmt=json"
        )
        req_details = urllib.request.Request(lookup_url, headers=config.MB_HEADERS)
        with urllib.request.urlopen(req_details, timeout=10) as resp:
            full_data = json.loads(resp.read().decode("utf-8"))

        tags = full_data.get("genres", []) or full_data.get("tags", [])
        genre_str = ""
        genre_list: List[str] = []
        if tags:
            tags.sort(key=lambda x: x.get("count", 0), reverse=True)
            valid = [
                t["name"].title()
                for t in tags
                if t.get("name", "").lower() not in ("music", "all")
            ]
            genre_list = valid[:5]
            genre_str = ", ".join(valid[:2])

        rel_prods, rel_comps, rel_arrs, rel_lyrics = _extract_mb_relations(full_data.get("relations", []))

        tracks: List[Dict[str, Any]] = []
        for disc_idx, medium in enumerate(full_data.get("media", []), 1):
            disc_num = medium.get("position", disc_idx)
            for trk in medium.get("tracks", []):
                rec = trk.get("recording", {})
                rec_prods, rec_comps, rec_arrs, rec_lyrics = _extract_mb_relations(rec.get("relations", []))

                trk_art = resolve_canonical_artist(trk.get("artist-credit", []), fallback_artist)

                tracks.append(
                    {
                        "position": trk.get("position"),
                        "disc_number": disc_num,
                        "title": trk.get("title", ""),
                        "artist": trk_art or fallback_artist,
                        "length": float(trk.get("length") or 0) / 1000.0,
                        "recording_id": rec.get("id"),
                        "producers": rec_prods or rel_prods,
                        "composers": rec_comps or rel_comps,
                        "arrangers": rec_arrs or rel_arrs,
                        "lyricists": rec_lyrics or rel_lyrics,
                    }
                )

        cover_data = fetch_cover_art_archive(rel_id)
        credits = full_data.get("artist-credit", [])
        artist_credit = resolve_canonical_artist(credits, fallback_artist)

        effective_genre = resolve_fallback_genre(
            artist_credit or fallback_artist, fallback_title, genre_str
        )
        if not genre_list and effective_genre:
            genre_list = [effective_genre]

        return {
            "mbid": rel_id,
            "title": full_data.get("title", fallback_title),
            "artist": artist_credit or fallback_artist,
            "date": full_data.get("date", ""),
            "genre": effective_genre,
            "genres": genre_list,
            "producers": rel_prods,
            "composers": rel_comps,
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

                credits = rec.get("artist-credit", [])
                artist_credit = resolve_canonical_artist(credits, primary_artist or artist)
                eff_genre = resolve_fallback_genre(
                    artist_credit or primary_artist or artist, title, genre_str
                )

                return {
                    "title": rec.get("title", title),
                    "artist": artist_credit or primary_artist or artist,
                    "genre": eff_genre,
                }
    except Exception as e:
        logger.warning(f"MusicBrainz track lookup failed for '{title}': {e}")
    return {}


# =====================================================================
# GENIUS CLIENT & CREDITS
# =====================================================================

def get_genius_client() -> Optional[lyricsgenius.Genius]:
    """Initializes and returns a Genius API client instance defensively."""
    if not getattr(config, "GENIUS_ACCESS_TOKEN", None):
        return None
    try:
        try:
            genius = lyricsgenius.Genius(
                config.GENIUS_ACCESS_TOKEN,
                timeout=12,
                retries=1,
            )
        except TypeError:
            genius = lyricsgenius.Genius(config.GENIUS_ACCESS_TOKEN)

        if hasattr(genius, "remove_section_headers"):
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


# =====================================================================
# MULTI-PROVIDER AGGREGATORS (ASYNC)
# =====================================================================

class MusicBrainzProvider:
    """Provider wrapper for MusicBrainz REST API & Cover Art Archive."""

    @staticmethod
    async def search_album(album: str, artist: str = "") -> List[UnifiedAlbumMetadata]:
        await _throttle_musicbrainz()
        results: List[UnifiedAlbumMetadata] = []
        try:
            loop = asyncio.get_running_loop()
            mb_data = await loop.run_in_executor(
                None, search_musicbrainz_release, album, artist
            )
            if not mb_data:
                return []

            tracks: List[UnifiedTrackMetadata] = []
            for trk in mb_data.get("tracks", []):
                tracks.append(
                    UnifiedTrackMetadata(
                        title=trk.get("title", ""),
                        artist=trk.get("artist") or mb_data.get("artist", artist),
                        album=mb_data.get("title", album),
                        track_number=int(trk.get("position", 1)),
                        total_tracks=len(mb_data.get("tracks", [])),
                        disc_number=int(trk.get("disc_number", 1)),
                        year=str(mb_data.get("date", ""))[:4],
                        genre=mb_data.get("genre", ""),
                        genres=mb_data.get("genres", []),
                        producers=trk.get("producers", mb_data.get("producers", [])),
                        composers=trk.get("composers", mb_data.get("composers", [])),
                        arrangers=trk.get("arrangers", []),
                        lyricists=trk.get("lyricists", []),
                        duration_seconds=float(trk.get("length", 0.0)),
                        source="MusicBrainz",
                        source_id=trk.get("recording_id", ""),
                    )
                )

            album_meta = UnifiedAlbumMetadata(
                album=mb_data.get("title", album),
                artist=mb_data.get("artist", artist),
                album_artist=mb_data.get("artist", artist),
                year=str(mb_data.get("date", "")),
                genre=mb_data.get("genre", ""),
                genres=mb_data.get("genres", []),
                total_tracks=len(tracks),
                cover_url=f"https://coverartarchive.org/release/{mb_data.get('mbid')}/front-500",
                cover_bytes=mb_data.get("cover_bytes"),
                tracks=tracks,
                producers=mb_data.get("producers", []),
                composers=mb_data.get("composers", []),
                source="MusicBrainz",
                source_id=mb_data.get("mbid", ""),
            )
            results.append(album_meta)
        except Exception as e:
            logger.debug(f"MusicBrainz provider error: {e}")
        return results

    @staticmethod
    async def search_track(title: str, artist: str = "") -> List[UnifiedTrackMetadata]:
        await _throttle_musicbrainz()
        results: List[UnifiedTrackMetadata] = []
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(
                None, search_musicbrainz_track, title, artist
            )
            if data and data.get("title"):
                results.append(
                    UnifiedTrackMetadata(
                        title=data.get("title", title),
                        artist=data.get("artist", artist),
                        genre=data.get("genre", ""),
                        source="MusicBrainz",
                    )
                )
        except Exception as e:
            logger.debug(f"MusicBrainz track lookup error: {e}")
        return results


class DeezerProvider:
    """Provider wrapper for public Deezer REST API."""

    @staticmethod
    async def search_album(album: str, artist: str = "") -> List[UnifiedAlbumMetadata]:
        results: List[UnifiedAlbumMetadata] = []
        query = f'artist:"{artist}" album:"{album}"' if artist else album
        url = f"https://api.deezer.com/search/album?q={urllib.parse.quote(query)}&limit=3"

        try:
            headers = {"Accept-Language": "en-US,en;q=0.9", "User-Agent": "AuraHub/1.0"}
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    return []
                data = resp.json().get("data", [])
                if not data and artist:
                    # Fallback to simple query
                    fallback_url = f"https://api.deezer.com/search/album?q={urllib.parse.quote(f'{artist} {album}')}&limit=3"
                    resp = await client.get(fallback_url, headers=headers)
                    data = resp.json().get("data", []) if resp.status_code == 200 else []

                for item in data[:2]:
                    album_id = item.get("id")
                    if not album_id:
                        continue
                    detail_resp = await client.get(f"https://api.deezer.com/album/{album_id}", headers=headers)
                    if detail_resp.status_code != 200:
                        continue
                    detail = detail_resp.json()

                    genre_list = [g.get("name") for g in detail.get("genres", {}).get("data", []) if g.get("name")]
                    producers: List[str] = []
                    composers: List[str] = []
                    for contrib in detail.get("contributors", []):
                        role = (contrib.get("role") or "").lower()
                        name = contrib.get("name") or ""
                        if not name:
                            continue
                        if "producer" in role and name not in producers:
                            producers.append(name)
                        elif "composer" in role and name not in composers:
                            composers.append(name)

                    dz_genre = resolve_fallback_genre(
                        detail.get("artist", {}).get("name", artist),
                        detail.get("title", album),
                        ", ".join(genre_list[:2]),
                    )

                    track_items = detail.get("tracks", {}).get("data", [])
                    tracks: List[UnifiedTrackMetadata] = []
                    for trk in track_items:
                        t_genre = resolve_fallback_genre(
                            trk.get("artist", {}).get("name") or detail.get("artist", {}).get("name", artist),
                            detail.get("title", album),
                            dz_genre,
                        )
                        tracks.append(
                            UnifiedTrackMetadata(
                                title=trk.get("title", ""),
                                artist=trk.get("artist", {}).get("name") or detail.get("artist", {}).get("name", artist),
                                album=detail.get("title", album),
                                track_number=int(trk.get("track_position", 1)),
                                total_tracks=len(track_items),
                                disc_number=int(trk.get("disk_number", 1)),
                                year=str(detail.get("release_date", ""))[:4],
                                genre=t_genre,
                                genres=[t_genre] if t_genre else [],
                                duration_seconds=float(trk.get("duration", 0)),
                                isrc=trk.get("isrc", ""),
                                cover_url=detail.get("cover_xl") or detail.get("cover_big") or "",
                                source="Deezer",
                                source_id=str(trk.get("id", "")),
                            )
                        )

                    cover_url = detail.get("cover_xl") or detail.get("cover_big") or ""
                    results.append(
                        UnifiedAlbumMetadata(
                            album=detail.get("title", album),
                            artist=detail.get("artist", {}).get("name", artist),
                            album_artist=detail.get("artist", {}).get("name", artist),
                            year=str(detail.get("release_date", "")),
                            genre=dz_genre,
                            genres=[dz_genre] if dz_genre else genre_list,
                            total_tracks=int(detail.get("nb_tracks", len(tracks))),
                            cover_url=cover_url,
                            tracks=tracks,
                            producers=producers,
                            composers=composers,
                            source="Deezer",
                            source_id=str(album_id),
                        )
                    )
        except Exception as e:
            logger.debug(f"Deezer album lookup failed: {e}")
        return results

    @staticmethod
    async def search_track(title: str, artist: str = "") -> List[UnifiedTrackMetadata]:
        results: List[UnifiedTrackMetadata] = []
        query = f'artist:"{artist}" track:"{title}"' if artist else title
        url = f"https://api.deezer.com/search/track?q={urllib.parse.quote(query)}&limit=3"

        try:
            headers = {"Accept-Language": "en-US,en;q=0.9", "User-Agent": "AuraHub/1.0"}
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    return []
                data = resp.json().get("data", [])
                for trk in data[:3]:
                    trk_genre = resolve_fallback_genre(
                        trk.get("artist", {}).get("name", artist),
                        trk.get("album", {}).get("title", ""),
                        "",
                    )
                    results.append(
                        UnifiedTrackMetadata(
                            title=trk.get("title", title),
                            artist=trk.get("artist", {}).get("name", artist),
                            album=trk.get("album", {}).get("title", ""),
                            genre=trk_genre,
                            genres=[trk_genre] if trk_genre else [],
                            duration_seconds=float(trk.get("duration", 0)),
                            cover_url=trk.get("album", {}).get("cover_xl") or trk.get("album", {}).get("cover_big", ""),
                            source="Deezer",
                            source_id=str(trk.get("id", "")),
                        )
                    )
        except Exception as e:
            logger.debug(f"Deezer track search failed: {e}")
        return results


class ITunesProvider:
    """Provider wrapper for public iTunes Search API."""

    @staticmethod
    async def search_album(album: str, artist: str = "") -> List[UnifiedAlbumMetadata]:
        results: List[UnifiedAlbumMetadata] = []
        term = f"{artist} {album}".strip()
        url = f"https://itunes.apple.com/search?term={urllib.parse.quote(term)}&entity=album&limit=3&lang=en_us"

        try:
            headers = {"Accept-Language": "en-US,en;q=0.9", "User-Agent": "AuraHub/1.0"}
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    return []
                candidates = resp.json().get("results", [])

                for item in candidates[:2]:
                    coll_id = item.get("collectionId")
                    if not coll_id:
                        continue

                    # Lookup full tracklist for album in English
                    lookup_url = f"https://itunes.apple.com/lookup?id={coll_id}&entity=song&lang=en_us"
                    l_resp = await client.get(lookup_url, headers=headers)
                    if l_resp.status_code != 200:
                        continue
                    l_data = l_resp.json().get("results", [])
                    track_items = [t for t in l_data if t.get("wrapperType") == "track"]

                    raw_cover = item.get("artworkUrl100", "")
                    highres_cover = raw_cover.replace("100x100bb.jpg", "1200x1200bb.jpg") if raw_cover else ""

                    itunes_genre = resolve_fallback_genre(
                        item.get("artistName", artist),
                        item.get("collectionName", album),
                        item.get("primaryGenreName", ""),
                    )

                    tracks: List[UnifiedTrackMetadata] = []
                    for t in track_items:
                        t_genre = resolve_fallback_genre(
                            t.get("artistName", artist),
                            t.get("collectionName", album),
                            t.get("primaryGenreName", itunes_genre),
                        )
                        tracks.append(
                            UnifiedTrackMetadata(
                                title=t.get("trackName", ""),
                                artist=t.get("artistName", artist),
                                album=t.get("collectionName", album),
                                track_number=int(t.get("trackNumber", 1)),
                                total_tracks=int(t.get("trackCount", len(track_items))),
                                disc_number=int(t.get("discNumber", 1)),
                                total_discs=int(t.get("discCount", 1)),
                                year=str(t.get("releaseDate", ""))[:4],
                                genre=t_genre,
                                genres=[t_genre] if t_genre else [],
                                duration_seconds=float(t.get("trackTimeMillis", 0)) / 1000.0,
                                cover_url=highres_cover,
                                source="iTunes",
                                source_id=str(t.get("trackId", "")),
                            )
                        )

                    results.append(
                        UnifiedAlbumMetadata(
                            album=item.get("collectionName", album),
                            artist=item.get("artistName", artist),
                            album_artist=item.get("artistName", artist),
                            year=str(item.get("releaseDate", ""))[:10],
                            genre=itunes_genre,
                            genres=[itunes_genre] if itunes_genre else [],
                            total_tracks=int(item.get("trackCount", len(tracks))),
                            cover_url=highres_cover,
                            tracks=tracks,
                            source="iTunes",
                            source_id=str(coll_id),
                        )
                    )
        except Exception as e:
            logger.debug(f"iTunes album search failed: {e}")
        return results

    @staticmethod
    async def search_track(title: str, artist: str = "") -> List[UnifiedTrackMetadata]:
        results: List[UnifiedTrackMetadata] = []
        term = f"{artist} {title}".strip()
        url = f"https://itunes.apple.com/search?term={urllib.parse.quote(term)}&entity=song&limit=3&lang=en_us"

        try:
            headers = {"Accept-Language": "en-US,en;q=0.9", "User-Agent": "AuraHub/1.0"}
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    return []
                candidates = resp.json().get("results", [])

                for t in candidates[:3]:
                    raw_cover = t.get("artworkUrl100", "")
                    highres_cover = raw_cover.replace("100x100bb.jpg", "1200x1200bb.jpg") if raw_cover else ""
                    trk_genre = resolve_fallback_genre(
                        t.get("artistName", artist),
                        t.get("collectionName", ""),
                        t.get("primaryGenreName", ""),
                    )
                    results.append(
                        UnifiedTrackMetadata(
                            title=t.get("trackName", title),
                            artist=t.get("artistName", artist),
                            album=t.get("collectionName", ""),
                            track_number=int(t.get("trackNumber", 1)),
                            total_tracks=int(t.get("trackCount", 1)),
                            disc_number=int(t.get("discNumber", 1)),
                            genre=trk_genre,
                            genres=[trk_genre] if trk_genre else [],
                            duration_seconds=float(t.get("trackTimeMillis", 0)) / 1000.0,
                            cover_url=highres_cover,
                            source="iTunes",
                            source_id=str(t.get("trackId", "")),
                        )
                    )
        except Exception as e:
            logger.debug(f"iTunes track search failed: {e}")
        return results


class SpotifyProvider:
    """Provider wrapper for Spotify Web API via client credentials flow."""

    _access_token: Optional[str] = None
    _token_expiry: float = 0.0

    @classmethod
    async def _get_token(cls) -> Optional[str]:
        client_id = getattr(config, "SPOTIFY_CLIENT_ID", "")
        client_secret = getattr(config, "SPOTIFY_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            return None

        now = time.time()
        if cls._access_token and now < cls._token_expiry - 60:
            return cls._access_token

        try:
            auth_str = f"{client_id}:{client_secret}"
            b64_auth = base64.b64encode(auth_str.encode()).decode()
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    "https://accounts.spotify.com/api/token",
                    headers={"Authorization": f"Basic {b64_auth}"},
                    data={"grant_type": "client_credentials"},
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    cls._access_token = payload.get("access_token")
                    cls._token_expiry = now + payload.get("expires_in", 3600)
                    return cls._access_token
        except Exception as e:
            logger.debug(f"Spotify token retrieval failed: {e}")
        return None

    @classmethod
    async def search_album(cls, album: str, artist: str = "") -> List[UnifiedAlbumMetadata]:
        token = await cls._get_token()
        if not token:
            return []

        results: List[UnifiedAlbumMetadata] = []
        query = f'album:"{album}" artist:"{artist}"' if artist else album
        url = f"https://api.spotify.com/v1/search?q={urllib.parse.quote(query)}&type=album&limit=2"

        try:
            headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en-US,en;q=0.9"}
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    return []
                items = resp.json().get("albums", {}).get("items", [])

                for item in items[:2]:
                    alb_id = item.get("id")
                    if not alb_id:
                        continue
                    detail_resp = await client.get(f"https://api.spotify.com/v1/albums/{alb_id}", headers=headers)
                    if detail_resp.status_code != 200:
                        continue
                    detail = detail_resp.json()

                    images = detail.get("images", [])
                    cover_url = images[0].get("url") if images else ""

                    sp_artist = ", ".join(a.get("name") for a in detail.get("artists", [])) or artist
                    sp_genre = resolve_fallback_genre(
                        sp_artist,
                        detail.get("name", album),
                        ", ".join(detail.get("genres", [])[:2]),
                    )

                    tracks: List[UnifiedTrackMetadata] = []
                    for t in detail.get("tracks", {}).get("items", []):
                        t_artists = [a.get("name") for a in t.get("artists", []) if a.get("name")]
                        tracks.append(
                            UnifiedTrackMetadata(
                                title=t.get("name", ""),
                                artist=", ".join(t_artists) or artist,
                                album=detail.get("name", album),
                                track_number=int(t.get("track_number", 1)),
                                total_tracks=int(detail.get("total_tracks", len(tracks))),
                                disc_number=int(t.get("disc_number", 1)),
                                year=str(detail.get("release_date", ""))[:4],
                                genre=sp_genre,
                                genres=[sp_genre] if sp_genre else [],
                                duration_seconds=float(t.get("duration_ms", 0)) / 1000.0,
                                cover_url=cover_url,
                                source="Spotify",
                                source_id=t.get("id", ""),
                            )
                        )

                    results.append(
                        UnifiedAlbumMetadata(
                            album=detail.get("name", album),
                            artist=sp_artist,
                            year=str(detail.get("release_date", "")),
                            genres=[sp_genre] if sp_genre else detail.get("genres", []),
                            genre=sp_genre,
                            total_tracks=int(detail.get("total_tracks", len(tracks))),
                            cover_url=cover_url,
                            tracks=tracks,
                            source="Spotify",
                            source_id=alb_id,
                        )
                    )
        except Exception as e:
            logger.debug(f"Spotify album lookup failed: {e}")
        return results

    @classmethod
    async def search_track(cls, title: str, artist: str = "") -> List[UnifiedTrackMetadata]:
        token = await cls._get_token()
        if not token:
            return []

        results: List[UnifiedTrackMetadata] = []
        query = f'track:"{title}" artist:"{artist}"' if artist else title
        url = f"https://api.spotify.com/v1/search?q={urllib.parse.quote(query)}&type=track&limit=3"

        try:
            headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en-US,en;q=0.9"}
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    return []
                items = resp.json().get("tracks", {}).get("items", [])
                for t in items[:3]:
                    images = t.get("album", {}).get("images", [])
                    cover_url = images[0].get("url") if images else ""
                    trk_artist = ", ".join(a.get("name") for a in t.get("artists", [])) or artist
                    trk_genre = resolve_fallback_genre(
                        trk_artist,
                        t.get("album", {}).get("name", ""),
                        "",
                    )
                    results.append(
                        UnifiedTrackMetadata(
                            title=t.get("name", title),
                            artist=trk_artist,
                            album=t.get("album", {}).get("name", ""),
                            genre=trk_genre,
                            genres=[trk_genre] if trk_genre else [],
                            duration_seconds=float(t.get("duration_ms", 0)) / 1000.0,
                            isrc=t.get("external_ids", {}).get("isrc", ""),
                            cover_url=cover_url,
                            source="Spotify",
                            source_id=t.get("id", ""),
                        )
                    )
        except Exception as e:
            logger.debug(f"Spotify track search failed: {e}")
        return results


class DiscogsProvider:
    """Provider wrapper for Discogs API (deep credits for producers and composers)."""

    @staticmethod
    async def search_album(album: str, artist: str = "") -> List[UnifiedAlbumMetadata]:
        token = getattr(config, "DISCOGS_API_TOKEN", "")
        if not token:
            return []

        results: List[UnifiedAlbumMetadata] = []
        query = f"{artist} {album}".strip()
        url = (
            f"https://api.discogs.com/database/search?q={urllib.parse.quote(query)}"
            f"&type=release&token={token}&per_page=2"
        )

        try:
            headers = {"User-Agent": "AuraMusicHub/1.0"}
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    return []
                candidates = resp.json().get("results", [])

                for item in candidates[:2]:
                    rel_id = item.get("id")
                    if not rel_id:
                        continue
                    detail_resp = await client.get(
                        f"https://api.discogs.com/releases/{rel_id}?token={token}",
                        headers=headers,
                    )
                    if detail_resp.status_code != 200:
                        continue
                    detail = detail_resp.json()

                    producers: List[str] = []
                    composers: List[str] = []
                    arrangers: List[str] = []
                    for extra in detail.get("extraartists", []):
                        role = (extra.get("role") or "").lower()
                        name = extra.get("name") or ""
                        if not name:
                            continue
                        if "producer" in role and name not in producers:
                            producers.append(name)
                        elif any(w in role for w in ("written", "composer", "music by")) and name not in composers:
                            composers.append(name)
                        elif "arrang" in role and name not in arrangers:
                            arrangers.append(name)

                    genres = detail.get("genres", []) + detail.get("styles", [])
                    images = detail.get("images", [])
                    cover_url = images[0].get("resource_url") if images else ""

                    disc_artist = detail.get("artists_sort") or artist
                    disc_genre = resolve_fallback_genre(
                        disc_artist,
                        detail.get("title", album),
                        ", ".join(genres[:2]),
                    )

                    tracks: List[UnifiedTrackMetadata] = []
                    for trk in detail.get("tracklist", []):
                        dur_str = trk.get("duration", "")
                        dur_sec = 0.0
                        if ":" in dur_str:
                            parts = dur_str.split(":")
                            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                                dur_sec = float(int(parts[0]) * 60 + int(parts[1]))

                        tracks.append(
                            UnifiedTrackMetadata(
                                title=trk.get("title", ""),
                                artist=disc_artist,
                                album=detail.get("title", album),
                                track_number=len(tracks) + 1,
                                duration_seconds=dur_sec,
                                genre=disc_genre,
                                genres=[disc_genre] if disc_genre else [],
                                producers=producers,
                                composers=composers,
                                arrangers=arrangers,
                                source="Discogs",
                            )
                        )

                    results.append(
                        UnifiedAlbumMetadata(
                            album=detail.get("title", album),
                            artist=disc_artist,
                            year=str(detail.get("year", "")),
                            genres=[disc_genre] if disc_genre else genres,
                            genre=disc_genre,
                            total_tracks=len(tracks),
                            cover_url=cover_url,
                            tracks=tracks,
                            producers=producers,
                            composers=composers,
                            source="Discogs",
                            source_id=str(rel_id),
                        )
                    )
        except Exception as e:
            logger.debug(f"Discogs lookup failed: {e}")
        return results


# =====================================================================
# LYRICS INTEGRATION HELPER (LRCLIB)
# =====================================================================

async def fetch_lrclib_lyrics_async(title: str, artist: str = "") -> Tuple[str, str]:
    """Queries LRCLIB for synced and unsynced lyrics text asynchronously."""
    params = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
    url = f"https://lrclib.net/api/get?{params}"
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(url, headers={"User-Agent": "AuraHub/1.0"})
            if resp.status_code == 200:
                data = resp.json()
                synced = data.get("syncedLyrics") or ""
                plain = data.get("plainLyrics") or ""
                return synced, plain
    except Exception:
        pass
    return "", ""


# =====================================================================
# RECOMMENDATION & SCORING ENGINE
# =====================================================================

def compute_confidence_score(
    query_title_or_album: str,
    query_artist: str,
    cand_title_or_album: str,
    cand_artist: str,
    local_track_count: Optional[int] = None,
    cand_track_count: Optional[int] = None,
    local_duration: Optional[float] = None,
    cand_duration: Optional[float] = None,
) -> float:
    """Computes a normalized confidence score (0.0 to 100.0) based on fuzzy similarity,

    phonetic variants, track count alignment, and duration proximity.
    """
    score = 0.0

    # 1. Artist matching (up to 35 points)
    query_art_variants = extract_clean_artists(query_artist) or [query_artist]
    cand_art_variants = extract_clean_artists(cand_artist) or [cand_artist]
    best_art_sim = 0.0
    for q_a in query_art_variants:
        for c_a in cand_art_variants:
            sim = calculate_string_similarity(q_a, c_a)
            if sim > best_art_sim:
                best_art_sim = sim
    score += (best_art_sim * 35.0)

    # 2. Title / Album matching with Franco-Arabic transliterations (up to 35 points)
    variants = franco_to_arabic(query_title_or_album) or [query_title_or_album]
    best_title_sim = calculate_best_variant_similarity(variants, cand_title_or_album)
    score += (best_title_sim * 35.0)

    # 3. Track count agreement (up to 20 points)
    if local_track_count is not None and cand_track_count is not None and local_track_count > 0:
        diff = abs(local_track_count - cand_track_count)
        if diff == 0:
            score += 20.0
        elif diff == 1:
            score += 12.0
        elif diff == 2:
            score += 6.0
    else:
        # If track count is not verified, give modest base points if present
        if cand_track_count and cand_track_count > 0:
            score += 10.0

    # 4. Duration proximity (up to 10 points)
    if local_duration and cand_duration and local_duration > 0 and cand_duration > 0:
        dur_diff = abs(local_duration - cand_duration)
        if dur_diff <= 3.0:
            score += 10.0
        elif dur_diff <= 8.0:
            score += 6.0
        elif dur_diff <= 15.0:
            score += 3.0
    else:
        score += 5.0

    return max(0.0, min(100.0, score))


def compute_metadata_completeness(
    album: Optional[UnifiedAlbumMetadata] = None,
    track: Optional[UnifiedTrackMetadata] = None,
) -> float:
    """Evaluates the depth and richness of extracted metadata tags (0.0 to 100.0)."""
    score = 0.0
    if album:
        if album.cover_url or album.cover_bytes:
            score += 20.0
        if album.total_tracks > 0 and album.tracks:
            score += 20.0
        if album.producers or any(t.producers for t in album.tracks):
            score += 15.0
        if album.composers or any(t.composers for t in album.tracks):
            score += 15.0
        if album.year:
            score += 10.0
        if album.genres or album.genre:
            score += 10.0
        if any(t.lyrics_synced or t.lyrics_unsynced for t in album.tracks):
            score += 10.0
    elif track:
        if track.cover_url or track.cover_bytes:
            score += 25.0
        if track.producers:
            score += 15.0
        if track.composers:
            score += 15.0
        if track.lyrics_synced or track.lyrics_unsynced:
            score += 15.0
        if track.year:
            score += 10.0
        if track.genres or track.genre:
            score += 10.0
        if track.duration_seconds > 0:
            score += 10.0
    return max(0.0, min(100.0, score))


def rank_and_recommend_candidates(candidates: List[MetadataCandidate]) -> List[MetadataCandidate]:
    """Ranks candidates by composite confidence & completeness and designates the best recommendation."""
    if not candidates:
        return []

    # Calculate composite score for each candidate
    scored: List[Tuple[float, MetadataCandidate]] = []
    for cand in candidates:
        compl = compute_metadata_completeness(cand.album_data, cand.track_data)
        # Composite score: 65% match confidence, 35% tag completeness
        composite = (cand.confidence_score * 0.65) + (compl * 0.35)
        scored.append((composite, cand))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Designate recommended candidate if top candidate meets confidence threshold
    top_composite, top_candidate = scored[0]
    recommended_set = False
    for _, cand in scored:
        cand.is_recommended = False
        if not recommended_set and cand.confidence_score >= 35.0:
            cand.is_recommended = True
            recommended_set = True

    return [cand for _, cand in scored]


# =====================================================================
# HIGH-LEVEL AGGREGATION APIS (ALBUMS & TRACKS)
# =====================================================================

async def search_album_metadata_candidates_async(
    album_name: str,
    artist_name: str = "",
    local_track_count: Optional[int] = None,
    local_durations: Optional[List[float]] = None,
) -> List[MetadataCandidate]:
    """Asynchronously queries all configured metadata providers and aggregates ranked album candidates."""
    tasks = [
        MusicBrainzProvider.search_album(album_name, artist_name),
        DeezerProvider.search_album(album_name, artist_name),
        ITunesProvider.search_album(album_name, artist_name),
        SpotifyProvider.search_album(album_name, artist_name),
        DiscogsProvider.search_album(album_name, artist_name),
    ]

    results_lists = await asyncio.gather(*tasks, return_exceptions=True)
    raw_albums: List[UnifiedAlbumMetadata] = []

    for res in results_lists:
        if isinstance(res, list):
            raw_albums.extend(res)

    candidates: List[MetadataCandidate] = []
    avg_local_dur = (sum(local_durations) / len(local_durations)) if local_durations else None

    for alb in raw_albums:
        cand_durations = [t.duration_seconds for t in alb.tracks if t.duration_seconds > 0]
        avg_cand_dur = (sum(cand_durations) / len(cand_durations)) if cand_durations else None

        conf = compute_confidence_score(
            query_title_or_album=album_name,
            query_artist=artist_name,
            cand_title_or_album=alb.album,
            cand_artist=alb.artist,
            local_track_count=local_track_count,
            cand_track_count=alb.total_tracks or len(alb.tracks),
            local_duration=avg_local_dur,
            cand_duration=avg_cand_dur,
        )

        has_prods = bool(alb.producers or any(t.producers for t in alb.tracks))
        has_comps = bool(alb.composers or any(t.composers for t in alb.tracks))
        has_lyrics = any(bool(t.lyrics_synced or t.lyrics_unsynced) for t in alb.tracks)

        preview = {
            "title": alb.album,
            "artist": alb.artist,
            "album": alb.album,
            "year": alb.year,
            "track_count": alb.total_tracks or len(alb.tracks),
            "genres": alb.genre or (", ".join(alb.genres[:2]) if alb.genres else "Unknown"),
            "has_cover": bool(alb.cover_url or alb.cover_bytes),
            "has_producers": has_prods,
            "has_composers": has_comps,
            "has_lyrics": has_lyrics,
            "cover_thumbnail": alb.cover_url,
            "confidence_percent": int(conf),
        }

        candidates.append(
            MetadataCandidate(
                source=alb.source,
                confidence_score=conf,
                is_recommended=False,
                album_data=alb,
                preview=preview,
            )
        )

    return rank_and_recommend_candidates(candidates)


def search_album_metadata_candidates(
    album_name: str,
    artist_name: str = "",
    local_track_count: Optional[int] = None,
    local_durations: Optional[List[float]] = None,
) -> List[MetadataCandidate]:
    """Synchronous interface for multi-provider album metadata search."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    coro = search_album_metadata_candidates_async(
        album_name, artist_name, local_track_count, local_durations
    )

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(coro)).result()
    else:
        return asyncio.run(coro)


async def search_track_metadata_candidates_async(
    title: str,
    artist: str = "",
    local_duration: Optional[float] = None,
) -> List[MetadataCandidate]:
    """Asynchronously queries metadata providers for single track candidates."""
    tasks = [
        MusicBrainzProvider.search_track(title, artist),
        DeezerProvider.search_track(title, artist),
        ITunesProvider.search_track(title, artist),
        SpotifyProvider.search_track(title, artist),
    ]

    results_lists = await asyncio.gather(*tasks, return_exceptions=True)
    raw_tracks: List[UnifiedTrackMetadata] = []

    for res in results_lists:
        if isinstance(res, list):
            raw_tracks.extend(res)

    candidates: List[MetadataCandidate] = []
    for trk in raw_tracks:
        conf = compute_confidence_score(
            query_title_or_album=title,
            query_artist=artist,
            cand_title_or_album=trk.title,
            cand_artist=trk.artist,
            local_duration=local_duration,
            cand_duration=trk.duration_seconds,
        )

        preview = {
            "title": trk.title,
            "artist": trk.artist,
            "album": trk.album,
            "year": trk.year,
            "genres": trk.genre,
            "has_cover": bool(trk.cover_url or trk.cover_bytes),
            "has_producers": bool(trk.producers),
            "has_composers": bool(trk.composers),
            "has_lyrics": bool(trk.lyrics_synced or trk.lyrics_unsynced),
            "cover_thumbnail": trk.cover_url,
            "confidence_percent": int(conf),
        }

        candidates.append(
            MetadataCandidate(
                source=trk.source,
                confidence_score=conf,
                is_recommended=False,
                track_data=trk,
                preview=preview,
            )
        )

    return rank_and_recommend_candidates(candidates)


def search_track_metadata_candidates(
    title: str,
    artist: str = "",
    local_duration: Optional[float] = None,
) -> List[MetadataCandidate]:
    """Synchronous interface for single track metadata search."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    coro = search_track_metadata_candidates_async(title, artist, local_duration)

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(coro)).result()
    else:
        return asyncio.run(coro)


async def fetch_lrclib_lyrics_async(track: str, artist: str = "") -> Dict[str, str]:
    """Queries LRCLIB for synced and plain lyrics."""
    if not track:
        return {"synced": "", "plain": ""}

    headers = getattr(config, "MB_HEADERS", {"User-Agent": "AuraHub/1.0"})
    artists_to_try = extract_clean_artists(artist) if artist else [""]
    if not artists_to_try:
        artists_to_try = [""]
    title_variants = franco_to_arabic(track)

    async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
        for art in artists_to_try:
            for tit in title_variants:
                # 1. Exact match attempt
                try:
                    params = {"track_name": tit}
                    if art:
                        params["artist_name"] = art
                    resp = await client.get("https://lrclib.net/api/get", params=params)
                    if resp.status_code == 200:
                        data = resp.json()
                        synced = data.get("syncedLyrics") or ""
                        plain = data.get("plainLyrics") or ""
                        if synced or plain:
                            return {"synced": synced, "plain": plain}
                except Exception:
                    pass

                # 2. Search fallback
                try:
                    query_str = f"{art} {tit}".strip()
                    resp = await client.get("https://lrclib.net/api/search", params={"q": query_str})
                    if resp.status_code == 200:
                        results = resp.json()
                        if isinstance(results, list):
                            for item in results:
                                synced = item.get("syncedLyrics") or ""
                                plain = item.get("plainLyrics") or ""
                                if synced or plain:
                                    return {"synced": synced, "plain": plain}
                except Exception:
                    pass

    return {"synced": "", "plain": ""}


async def search_metadata_async(
    query: str,
    type: str = "album",
    artist: str = "",
) -> List[Dict[str, Any]]:
    """Unified metadata search querying MusicBrainz, Deezer, Spotify, iTunes, Discogs, and LRCLIB.

    Calculates match confidence (0-100%), flags top candidate as recommended: true,
    and returns normalized field payloads ready to populate the frontend studio form.
    """
    clean_q = query.strip()
    clean_art = artist.strip()
    is_track = type.lower() == "track"

    candidates: List[MetadataCandidate] = []
    if is_track:
        cand_task = search_track_metadata_candidates_async(title=clean_q, artist=clean_art)
        lrc_task = fetch_lrclib_lyrics_async(track=clean_q, artist=clean_art)
        cand_res, lrc_res = await asyncio.gather(cand_task, lrc_task, return_exceptions=True)

        if isinstance(cand_res, list):
            candidates = cand_res

        synced_lyrics = lrc_res.get("synced", "") if isinstance(lrc_res, dict) else ""
        plain_lyrics = lrc_res.get("plain", "") if isinstance(lrc_res, dict) else ""

        if synced_lyrics or plain_lyrics:
            for c in candidates:
                if c.track_data:
                    if not c.track_data.lyrics_synced and synced_lyrics:
                        c.track_data.lyrics_synced = synced_lyrics
                    if not c.track_data.lyrics_unsynced and plain_lyrics:
                        c.track_data.lyrics_unsynced = plain_lyrics
                    c.preview["has_lyrics"] = True

            if not candidates and (synced_lyrics or plain_lyrics):
                conf = 85.0
                cand_lrclib = MetadataCandidate(
                    source="LRCLIB",
                    confidence_score=conf,
                    is_recommended=True,
                    track_data=UnifiedTrackMetadata(
                        title=clean_q,
                        artist=clean_art,
                        lyrics_synced=synced_lyrics,
                        lyrics_unsynced=plain_lyrics,
                        source="LRCLIB",
                    ),
                    preview={
                        "title": clean_q,
                        "artist": clean_art,
                        "album": "",
                        "year": "",
                        "genres": "",
                        "has_cover": False,
                        "has_producers": False,
                        "has_composers": False,
                        "has_lyrics": True,
                        "cover_thumbnail": "",
                        "confidence_percent": int(conf),
                    },
                )
                candidates.append(cand_lrclib)
    else:
        cand_res = await search_album_metadata_candidates_async(album_name=clean_q, artist_name=clean_art)
        if isinstance(cand_res, list):
            candidates = cand_res

    # Rank candidates and set recommendation
    ranked = rank_and_recommend_candidates(candidates)

    output_list: List[Dict[str, Any]] = []
    for idx, c in enumerate(ranked):
        c_dict = c.to_dict()
        is_rec = bool(idx == 0 and c.confidence_score >= 40.0)
        c_dict["is_recommended"] = is_rec
        c_dict["recommended"] = is_rec

        fields: Dict[str, Any] = {}
        if c.track_data:
            t = c.track_data
            fields = {
                "title": t.title,
                "artist": t.artist,
                "album": t.album,
                "album_artist": t.album_artist or t.artist,
                "track_number": t.track_number,
                "total_tracks": t.total_tracks,
                "disc_number": t.disc_number,
                "total_discs": t.total_discs,
                "year": t.year,
                "date": t.year,
                "genre": t.genre or (", ".join(t.genres[:2]) if t.genres else ""),
                "composers": ", ".join(t.composers) if t.composers else "",
                "producers": ", ".join(t.producers) if t.producers else "",
                "arrangers": ", ".join(t.arrangers) if t.arrangers else "",
                "lyricists": ", ".join(t.lyricists) if t.lyricists else "",
                "lyrics_synced": t.lyrics_synced,
                "lyrics_unsynced": t.lyrics_unsynced,
                "cover_url": t.cover_url,
            }
        elif c.album_data:
            a = c.album_data
            fields = {
                "title": a.album,
                "artist": a.artist,
                "album": a.album,
                "album_artist": a.album_artist or a.artist,
                "track_number": 1,
                "total_tracks": a.total_tracks or len(a.tracks) or 1,
                "disc_number": 1,
                "total_discs": a.total_discs or 1,
                "year": a.year,
                "date": a.year,
                "genre": a.genre or (", ".join(a.genres[:2]) if a.genres else ""),
                "composers": ", ".join(a.composers) if a.composers else "",
                "producers": ", ".join(a.producers) if a.producers else "",
                "arrangers": "",
                "lyricists": "",
                "lyrics_synced": "",
                "lyrics_unsynced": "",
                "cover_url": a.cover_url,
            }

        c_dict["fields"] = fields
        output_list.append(c_dict)

    return output_list


def search_metadata(
    query: str,
    type: str = "album",
    artist: str = "",
) -> List[Dict[str, Any]]:
    """Synchronous interface for unified metadata search."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    coro = search_metadata_async(query, type, artist)
    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(coro)).result()
    else:
        return asyncio.run(coro)

