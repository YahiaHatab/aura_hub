"""Configuration settings for Aura Hub.

Supports loading from environment variables with sensible defaults.
"""

import os
from pathlib import Path

# Load .env file automatically if present
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.is_file():
    try:
        with open(_env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _key, _val = _line.split("=", 1)
                _key = _key.strip()
                _val = _val.strip().strip("'\"")
                if _key and _key not in os.environ:
                    os.environ[_key] = _val
    except Exception:
        pass

# ================= TELEGRAM CONFIGURATION =================
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "8931396589:AAEFjxpKr6AC5dEbcxt61QADWVXqlxVjpKI"
)

ADMIN_TOKEN: str = os.environ.get("ADMIN_TOKEN", "")

# Parse allowed and admin user IDs as sets
_raw_allowed = os.environ.get("ALLOWED_USER_IDS", "1497076788")
ALLOWED_USER_IDS: set[int] = {
    int(uid.strip())
    for uid in _raw_allowed.split(",")
    if uid.strip().isdigit()
}

_raw_admins = os.environ.get("ADMIN_USER_IDS", "")
if _raw_admins.strip():
    ADMIN_USER_IDS: set[int] = {
        int(uid.strip())
        for uid in _raw_admins.split(",")
        if uid.strip().isdigit()
    }
else:
    ADMIN_USER_IDS = set(ALLOWED_USER_IDS)

# Ensure all admins are also included in the allowed users set
ALLOWED_USER_IDS.update(ADMIN_USER_IDS)

# Primary admin ID for desktop/fallback contexts
PRIMARY_ADMIN_ID: int = next(iter(ADMIN_USER_IDS)) if ADMIN_USER_IDS else 0

GENIUS_ACCESS_TOKEN = os.environ.get(
    "GENIUS_ACCESS_TOKEN",
    "dNjPb3-f4YurUlKQ6kbIRHbm80uBxfBsRoKomEDgpnaLgScIavTO6e9_p19rUbFI",
)
ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY", "cSpUJKpD")
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
DISCOGS_API_TOKEN = os.environ.get("DISCOGS_API_TOKEN", "")

# ================= METADATA PROVIDERS CONFIGURATION =================
MB_RATE_LIMIT_DELAY = float(os.environ.get("MB_RATE_LIMIT_DELAY", "1.0"))
METADATA_HTTP_TIMEOUT = float(os.environ.get("METADATA_HTTP_TIMEOUT", "8.0"))
LRCLIB_API_URL = os.environ.get("LRCLIB_API_URL", "https://lrclib.net").rstrip("/")
DEEZER_API_URL = os.environ.get("DEEZER_API_URL", "https://api.deezer.com").rstrip("/")
ITUNES_API_URL = os.environ.get("ITUNES_API_URL", "https://itunes.apple.com").rstrip("/")
ID3_V2_VERSION = int(os.environ.get("ID3_V2_VERSION", "4"))


# ================= STORAGE & PATHS =================
_raw_music_dir = os.environ.get("BASE_DOWNLOAD_DIR", os.path.expanduser("~/Music"))
BASE_DOWNLOAD_DIR = Path(_raw_music_dir).expanduser().resolve()

# ================= NAVIDROME / SUBSONIC API =================
NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "http://localhost:4533").rstrip("/")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "")
NAVIDROME_PASS = os.environ.get("NAVIDROME_PASS", "")

# ================= CLIENT CONSTANTS & HEADERS =================
MB_HEADERS = {"User-Agent": "AuraMusicHub/1.0 (contact@aurahub.local)"}
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "6"))

# ================= TELEGRAM WEBAPP / MINI APP =================
WEBAPP_HOST = os.environ.get("WEBAPP_HOST", "127.0.0.1")
WEBAPP_PORT = int(os.environ.get("WEBAPP_PORT", "8000"))
WEBAPP_EXTERNAL_URL = os.environ.get("WEBAPP_EXTERNAL_URL", "").rstrip("/")

# ================= AUDIO EXTENSIONS =================
AUDIO_EXTENSIONS = ('.mp3', '.flac', '.opus', '.m4a')


