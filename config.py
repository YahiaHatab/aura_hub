"""Configuration settings for Aura Hub.

Supports loading from environment variables with sensible defaults.
"""

import os
from pathlib import Path

# ================= TELEGRAM CONFIGURATION =================
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "8931396589:AAEFjxpKr6AC5dEbcxt61QADWVXqlxVjpKI"
)

# Parse allowed user IDs (comma-separated string or single integer)
_raw_allowed = os.environ.get("ALLOWED_USER_IDS", "1497076788")
ALLOWED_USER_IDS = [
    int(uid.strip())
    for uid in _raw_allowed.split(",")
    if uid.strip().isdigit()
]

# ================= API KEYS & SECRETS =================
GENIUS_ACCESS_TOKEN = os.environ.get(
    "GENIUS_ACCESS_TOKEN",
    "dNjPb3-f4YurUlKQ6kbIRHbm80uBxfBsRoKomEDgpnaLgScIavTO6e9_p19rUbFI",
)
ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY", "cSpUJKpD")

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
