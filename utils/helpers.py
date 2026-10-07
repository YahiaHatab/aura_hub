"""String processing, transliteration, and sanitization helpers for Aura Hub."""

import re
import urllib.parse
from typing import Any, Dict, List, Optional

from config import AUDIO_EXTENSIONS


def get_clean_name(name: str) -> str:
    """Strips leading track numbers and punctuation for clean token matching."""
    if not name:
        return ""
    clean = re.sub(r"^\d+\s*[-.]\s*", "", name)
    clean = re.sub(r"[\(\)\[\]\-_,.]", " ", clean)
    return " ".join(clean.lower().split())


def extract_clean_artists(raw_artist: str) -> List[str]:
    """Splits bilingual or combined artist strings into separate search candidates.

    Example: "Mohamed Mounir  محمد منير" -> ["Mohamed Mounir", "محمد منير"]
    Avoids broken quotes and Lucene syntax errors when querying MusicBrainz.
    """
    candidates: List[str] = []
    if not raw_artist or raw_artist.lower() in (
        "unknown artist",
        "various",
        "music",
        "youtube",
        "various artists",
    ):
        return candidates

    arabic_part = " ".join(re.findall(r"[\u0600-\u06FF]+", raw_artist)).strip()
    latin_part = " ".join(re.findall(r"[a-zA-Z0-9]+", raw_artist)).strip()

    if latin_part:
        candidates.append(latin_part)
    if arabic_part:
        candidates.append(arabic_part)

    raw_stripped = raw_artist.strip()
    if raw_stripped and raw_stripped not in candidates:
        candidates.append(raw_stripped)

    return candidates


def franco_to_arabic(text: str) -> List[str]:
    """Translates Franco-Arabic digits and phonetics into Arabic script and normalized Latin.

    Generates search variations to match official database entries (e.g. 3 -> ع, 7 -> ح).
    """
    if not text:
        return []

    variations: List[str] = [text]
    lower_text = text.lower()

    # Whole-word dictionary substitutions
    dict_map = {
        r"\bfi\b": "في",
        r"\bfe\b": "في",
        r"\bel\b": "ال",
        r"\bal\b": "ال",
        r"\b3esh2\b": "عشق",
        r"\b3eshq\b": "عشق",
        r"\beshq\b": "عشق",
        r"\bbanat\b": "بنات",
        r"\bhabibi\b": "حبيبي",
        r"\b7abibi\b": "حبيبي",
        r"\bana\b": "انا",
        r"\benta\b": "انتا",
        r"\benti\b": "انتي",
        r"\bleh\b": "ليه",
        r"\bya\b": "يا",
        r"\bmen\b": "من",
        r"\bkol\b": "كل",
        r"\baaks\b": "عكس",
        r"\b3aks\b": "عكس",
        r"\bshayfenha\b": "شايفينها",
        r"\bshayfeenha\b": "شايفينها",
        r"\bsaharna\b": "سهرنا",
        r"\bleil\b": "ليل",
        r"\blail\b": "ليل",
    }

    arabic_draft = lower_text
    for pattern, replacement in dict_map.items():
        arabic_draft = re.sub(pattern, replacement, arabic_draft)

    char_map = {
        "3": "ع",
        "7": "ح",
        "2": "أ",
        "5": "خ",
        "6": "ط",
        "8": "غ",
        "sh": "ش",
        "kh": "خ",
        "th": "ث",
        "b": "ب",
        "t": "ت",
        "j": "ج",
        "d": "د",
        "r": "ر",
        "z": "ز",
        "s": "س",
        "f": "ف",
        "q": "ق",
        "k": "ك",
        "l": "ل",
        "m": "م",
        "n": "ن",
        "h": "ه",
        "w": "و",
        "y": "ي",
    }

    # Digraphs first
    for k in ("sh", "kh", "th"):
        arabic_draft = arabic_draft.replace(k, char_map[k])

    # Single characters
    for k, v in char_map.items():
        arabic_draft = arabic_draft.replace(k, v)

    # Strip remaining untranslated Latin characters
    arabic_cleaned = re.sub(r"[a-z]", "", arabic_draft).strip()
    if arabic_cleaned and arabic_cleaned not in variations:
        variations.append(arabic_cleaned)

    # Latin normalized variant (numbers to English phonetics)
    latin_clean = lower_text
    substitutions = {
        "3": "e",
        "7": "h",
        "2": "a",
        "5": "kh",
        "6": "t",
        "8": "g",
    }
    for num, char in substitutions.items():
        latin_clean = latin_clean.replace(num, char)

    if latin_clean not in variations:
        variations.append(latin_clean)

    return variations


def parse_genius_input(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Extracts artist and album or album_id from a Genius URL or raw string."""
    if not text:
        return None
    text = text.strip()
    if "genius.com" in text or text.startswith("http"):
        url_split = urllib.parse.urlsplit(text)
        path = url_split.path

        album_match = re.search(r"/albums/([^/]+)/([^/?#]+)", path)
        if album_match:
            artist = urllib.parse.unquote(album_match.group(1)).replace("-", " ").strip()
            album = urllib.parse.unquote(album_match.group(2)).replace("-", " ").strip()
            album = re.sub(r"\s+lyrics$", "", album, flags=re.IGNORECASE)
            return {"artist": artist, "album": album}

        id_match = re.search(r"/albums/(\d+)", path)
        if id_match:
            return {"album_id": int(id_match.group(1))}

    return None


def sanitize_filename(name: str) -> str:
    """Removes invalid filesystem characters across Windows and Linux."""
    if not name:
        return "Unknown"
    # Remove characters forbidden on Windows/Linux filesystems: \ / : * ? " < > |
    sanitized = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    # Normalize multiple whitespaces
    sanitized = re.sub(r"\s+", " ", sanitized)
    return sanitized or "Unknown"


def format_bytes(size_bytes: int | float) -> str:
    """Formats bytes into human readable format (KB, MB, GB)."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"
