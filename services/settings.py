"""Settings management for Aura Hub.

Persists user and global quality preferences (auto, opus, mp3, flac) in data/settings.json.
"""

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"
_settings_lock = threading.RLock()

VALID_QUALITIES = ("auto", "opus", "mp3", "flac")

QUALITY_LABELS: Dict[str, str] = {
    "auto": "💎 Auto (Lossless FLAC -> Opus Fallback)",
    "opus": "🎧 Pure Opus (Fast & Native 160k)",
    "mp3": "🎵 Pure MP3 (Standard 320k)",
    "flac": "✨ Lossless FLAC (Strict)",
}

DEFAULT_SETTINGS: Dict[str, Any] = {
    "default_quality": "auto",
    "user_qualities": {},
}


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> Dict[str, Any]:
    """Loads settings dictionary from data/settings.json in a thread-safe manner."""
    with _settings_lock:
        _ensure_data_dir()
        if not SETTINGS_FILE.is_file():
            return dict(DEFAULT_SETTINGS)
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    return dict(DEFAULT_SETTINGS)
                if "default_quality" not in data:
                    data["default_quality"] = "auto"
                if "user_qualities" not in data or not isinstance(data["user_qualities"], dict):
                    data["user_qualities"] = {}
                return data
        except Exception as e:
            logger.warning(f"Failed to read settings from {SETTINGS_FILE}: {e}")
            return dict(DEFAULT_SETTINGS)


def save_settings(settings: Dict[str, Any]) -> None:
    """Saves settings dictionary to data/settings.json in a thread-safe manner."""
    with _settings_lock:
        _ensure_data_dir()
        temp_file = SETTINGS_FILE.with_suffix(".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
            temp_file.replace(SETTINGS_FILE)
        except Exception as e:
            logger.error(f"Failed to write settings to {SETTINGS_FILE}: {e}")
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass


def get_quality_preference(user_id: Optional[Union[int, str]] = None) -> str:
    """Returns the effective quality preference for a given user or global default."""
    settings = load_settings()
    if user_id is not None:
        user_qual = settings.get("user_qualities", {}).get(str(user_id))
        if user_qual in VALID_QUALITIES:
            return user_qual
    global_qual = settings.get("default_quality", "auto")
    return global_qual if global_qual in VALID_QUALITIES else "auto"


def set_quality_preference(
    quality: str, user_id: Optional[Union[int, str]] = None, set_global: bool = True
) -> str:
    """Sets quality preference for a user or globally.

    Returns the normalized quality string.
    """
    clean_qual = quality.strip().lower()
    if clean_qual not in VALID_QUALITIES:
        raise ValueError(
            f"Invalid quality '{quality}'. Supported: {', '.join(VALID_QUALITIES)}"
        )

    with _settings_lock:
        settings = load_settings()
        if user_id is not None:
            settings.setdefault("user_qualities", {})[str(user_id)] = clean_qual
        if set_global or user_id is None:
            settings["default_quality"] = clean_qual
        save_settings(settings)

    return clean_qual


def parse_quality_flag(text: str) -> Tuple[str, Optional[str]]:
    """Parses optional --flac, --opus, --mp3, or --auto flag from command text.

    Returns (clean_text_without_flag, quality_flag_or_none).
    """
    if not text:
        return "", None

    pattern = r"(?:^|\s)--(flac|opus|mp3|auto)\b"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return text.strip(), None

    flag = match.group(1).lower()
    # Strip the matched flag from text
    cleaned = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
    # Normalize multiple whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, flag
