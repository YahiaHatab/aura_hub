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


def is_arabic_music(artist: str = "", album: str = "", text: str = "") -> bool:
    """Detects if artist/album or text corresponds to Arabic music."""
    combined = f"{artist} {album} {text}".strip()
    if not combined:
        return False
    # Arabic script characters
    if re.search(r"[\u0600-\u06FF]", combined):
        return True
    # Common Arabic artist tokens in Latin/Franco script
    known_arabic_tokens = {
        "elissa", "amr diab", "mohamed mounir", "cairokee", "hamaki", "tamer hosny",
        "nancy ajram", "sherine", "angham", "fairuz", "fayrouz", "umm kulthum",
        "om kalthoum", "abdel halim", "hafez", "mohamed abdo", "assala", "ragheb alama",
        "george wassouf", "wael kfoury", "marwan pablo", "wegz", "marwan moussa",
        "abyusif", "shabjdeed", "al shami", "esseily", "balqees", "jassmi", "kadim",
        "saber rebai", "cheb khaled", "cheb mami", "saad lamjarred", "asma lmnawar",
        "ziad rahbani", "haifa wehbe", "myriam fares", "carole samaha", "nawal el zoghbi",
        "fadl shaker", "majed al mohandis", "mahmoud el esseily", "ahmed saad", "rotana",
        "mazzika", "leil", "lail", "saharna", "habibi", "7abibi", "kol hayaty"
    }
    low = combined.lower()
    return any(tok in low for tok in known_arabic_tokens)


# Comprehensive translation dictionary mapping Arabic genre terms to standard English
ARABIC_GENRE_MAP: Dict[str, str] = {
    # Pop
    "موسيقى البوب": "Pop",
    "البوب": "Pop",
    "بوب": "Pop",
    "بوب عربي": "Arabic Pop",
    "موسيقى البوب العربي": "Arabic Pop",
    "بوب غربي": "Western Pop",
    "كي بوب": "K-Pop",
    "كي-بوب": "K-Pop",
    "جي بوب": "J-Pop",
    # Rock / Metal
    "موسيقى الروك": "Rock",
    "الروك": "Rock",
    "روك": "Rock",
    "هارد روك": "Hard Rock",
    "موسيقى الميتال": "Metal",
    "الميتال": "Metal",
    "ميتال": "Metal",
    "هيفي ميتال": "Heavy Metal",
    "بانك": "Punk",
    "جرانج": "Grunge",
    # Hip-Hop / Rap / Trap
    "موسيقى الهيب هوب": "Hip-Hop/Rap",
    "الهيب هوب": "Hip-Hop/Rap",
    "هيب هوب": "Hip-Hop/Rap",
    "موسيقى الراب": "Hip-Hop/Rap",
    "الراب": "Hip-Hop/Rap",
    "راب": "Hip-Hop/Rap",
    "هيب هوب/راب": "Hip-Hop/Rap",
    "تراب": "Trap",
    "دريل": "Drill",
    # Electronic / Dance / House
    "موسيقى إلكترونية": "Electronic",
    "موسيقى الكترونية": "Electronic",
    "إلكتروني": "Electronic",
    "الكتروني": "Electronic",
    "موسيقى الرقص": "Dance",
    "الرقص": "Dance",
    "رقص": "Dance",
    "هاوس": "House",
    "ديب هاوس": "Deep House",
    "تكنو": "Techno",
    "ترانس": "Trance",
    "دانس": "Dance",
    "إي دي إم": "EDM",
    # R&B / Soul / Funk
    "آر أند بي": "R&B/Soul",
    "آر اند بي": "R&B/Soul",
    "أر أند بي": "R&B/Soul",
    "السول": "Soul",
    "سول": "Soul",
    "موسيقى السول": "Soul",
    "فانك": "Funk",
    # Classical / Instrumental / Soundtrack
    "موسيقى كلاسيكية": "Classical",
    "كلاسيكي": "Classical",
    "كلاسيكية": "Classical",
    "موسيقى تصويرية": "Soundtrack",
    "موسيقى فيلم": "Soundtrack",
    "أوركسترا": "Orchestral",
    "سمفوني": "Symphonic",
    "آلات موسيقية": "Instrumental",
    # Jazz / Blues
    "موسيقى الجاز": "Jazz",
    "الجاز": "Jazz",
    "جاز": "Jazz",
    "موسيقى البلوز": "Blues",
    "البلوز": "Blues",
    "بلوز": "Blues",
    # Country / Folk
    "موسيقى الريف": "Country",
    "الريف": "Country",
    "كانتري": "Country",
    "موسيقى الفولك": "Folk",
    "فولك": "Folk",
    "موسيقى شعبية": "Folk",
    # Alternative / Indie
    "موسيقى بديلة": "Alternative",
    "بديل": "Alternative",
    "إندي": "Indie",
    "اندي": "Indie",
    "أندرجراوند": "Indie",
    # Arabic / Regional styles in Latin English
    "موسيقى عربية": "Arabic Pop",
    "عربي": "Arabic Pop",
    "موسيقى شرقية": "Arabic",
    "طرب": "Tarab",
    "موسيقى الطرب": "Tarab",
    "شعبي": "Shaabi",
    "شعبي مصري": "Shaabi",
    "مهرجانات": "Mahraganat",
    "خليجي": "Khaleeji",
    "مغاربي": "Maghrebi",
    "راي": "Rai",
    "صوفي": "Sufi",
    "أندلسي": "Andalusian",
    "نوبي": "Nubian",
    "تقليدي": "Traditional",
    "موسيقى العالم": "World",
    "عالمي": "World",
    "لاتيني": "Latin",
    "ريغي": "Reggae",
    "أطفال": "Children's Music",
    "أغاني أطفال": "Children's Music",
    "ساوند تراك": "Soundtrack",
    "دبكة": "Dabke",
    "إنشاد": "Nasheed",
    "انشاد": "Nasheed",
    "فولكلور": "Folk",
    "أندرجراوند": "Indie",
}


def normalize_genre(genre_str: str) -> str:
    """Normalizes genre strings to English, translating any localized Arabic genres."""
    if not genre_str:
        return ""

    raw = genre_str.strip()

    # If it contains parentheses e.g. "Pop (موسيقى البوب)" or "موسيقى البوب (Pop)"
    paren_match = re.search(r"\(([^)]+)\)", raw)
    if paren_match:
        inside = paren_match.group(1).strip()
        outside = re.sub(r"\([^)]*\)", "", raw).strip()
        # If outside has English letters, prefer outside
        if re.search(r"[a-zA-Z]", outside):
            return normalize_genre(outside)
        # If inside has English letters, prefer inside
        if re.search(r"[a-zA-Z]", inside):
            return normalize_genre(inside)

    # Handle multiple genres separated by delimiters
    for delim in (",", "/", ";", "&", " - "):
        if delim in raw:
            parts = [normalize_genre(p.strip()) for p in raw.split(delim) if p.strip()]
            valid_parts = [p for p in parts if p]
            # Deduplicate preserving order
            seen: Set[str] = set()
            unique_parts = []
            for p in valid_parts:
                p_lower = p.lower()
                if p_lower not in seen:
                    seen.add(p_lower)
                    unique_parts.append(p)
            if unique_parts:
                join_str = ", " if delim in (",", ";") else f" {delim.strip()} "
                return join_str.join(unique_parts)

    cleaned = raw
    norm_key = re.sub(r"[^\w\s]", "", cleaned).strip().lower()

    # 1. Exact match in translation dictionary
    if cleaned in ARABIC_GENRE_MAP:
        return ARABIC_GENRE_MAP[cleaned]
    for k, v in ARABIC_GENRE_MAP.items():
        if norm_key == re.sub(r"[^\w\s]", "", k).strip().lower():
            return v

    # 2. Check if Arabic characters are present
    if re.search(r"[\u0600-\u06FF]", cleaned):
        # If it has English characters, strip out the Arabic characters first
        latin_only = re.sub(r"[\u0600-\u06FF]", "", cleaned).strip()
        latin_clean = re.sub(r"^[^\w]+|[^\w]+$", "", latin_only).strip()
        if len(latin_clean) >= 2 and re.search(r"[a-zA-Z]", latin_clean):
            return latin_clean

        low = cleaned.lower()
        if "بوب" in low:
            return "Arabic Pop" if "عرب" in low else "Pop"
        if "روك" in low:
            return "Rock"
        if "ميتال" in low:
            return "Metal"
        if "راب" in low or "هيب" in low or "تراب" in low or "دريل" in low:
            return "Hip-Hop/Rap"
        if "إلكترون" in low or "الكترون" in low or "تكنو" in low or "هاوس" in low:
            return "Electronic"
        if "رقص" in low or "دانس" in low:
            return "Dance"
        if "جاز" in low:
            return "Jazz"
        if "بلوز" in low:
            return "Blues"
        if "كلاسيك" in low:
            return "Classical"
        if "تصوير" in low or "تراك" in low:
            return "Soundtrack"
        if "شعب" in low:
            return "Shaabi"
        if "مهرجان" in low:
            return "Mahraganat"
        if "طرب" in low:
            return "Tarab"
        if "خليج" in low:
            return "Khaleeji"
        if "راي" in low:
            return "Rai"
        if "صوفي" in low or "انشاد" in low or "إنشاد" in low:
            return "Sufi"
        if "دبك" in low:
            return "Dabke"
        if "عرب" in low:
            return "Arabic Pop"
        # Fallback for unmapped Arabic text
        return "Arabic Pop"

    return cleaned


def resolve_fallback_genre(artist: str = "", album: str = "", default: str = "") -> str:
    """Returns an authentic English genre even if providers return Arabic, empty, or generic 'Music'."""
    cand = normalize_genre((default or "").strip())
    if cand and cand.lower() not in ("music", "all", "unknown", "other", "soundtrack", "various", ""):
        # Strict safeguard: ensure no Arabic characters remain
        if not re.search(r"[\u0600-\u06FF]", cand):
            return cand

    if is_arabic_music(artist, album):
        return "Arabic Pop"
    return "Pop"


