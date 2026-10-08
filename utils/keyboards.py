"""Dynamic inline keyboards for pagination, search results, and confirmation dialogs."""

from typing import Any, Dict, List
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config


def build_folder_keyboard(
    folders: List[str],
    prefix: str = "retag",
    page: int = 0,
    page_size: int = config.PAGE_SIZE,
) -> InlineKeyboardMarkup:
    """Builds a paginated inline keyboard for browsing album folders."""
    total_folders = len(folders)
    total_pages = max(1, (total_folders + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * page_size
    end_idx = start_idx + page_size
    page_folders = folders[start_idx:end_idx]

    keyboard: List[List[InlineKeyboardButton]] = []

    for idx, folder in enumerate(page_folders):
        global_idx = start_idx + idx
        # Normalize slashes for neat cross-platform display
        parts = folder.replace("\\", "/").strip("/").split("/")
        display_name = "/".join(parts[-2:]) if len(parts) >= 2 else (parts[0] if parts else folder)
        btn_text = f"📁 {display_name[:38]}"
        keyboard.append(
            [InlineKeyboardButton(btn_text, callback_data=f"{prefix}_sel:{global_idx}")]
        )

    # Navigation row
    nav_row: List[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}_page:{page - 1}")
        )
    nav_row.append(
        InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
    )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}_page:{page + 1}")
        )

    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="folder_cancel")])
    return InlineKeyboardMarkup(keyboard)


def build_confirmation_keyboard(
    confirm_callback: str,
    cancel_callback: str = "folder_cancel",
    confirm_text: str = "✅ Yes, Permanently Delete",
    cancel_text: str = "❌ Cancel",
) -> InlineKeyboardMarkup:
    """Builds a two-button confirmation dialog."""
    keyboard = [
        [InlineKeyboardButton(confirm_text, callback_data=confirm_callback)],
        [InlineKeyboardButton(cancel_text, callback_data=cancel_callback)],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_search_results_keyboard(
    results: List[Dict[str, Any]],
    prefix: str = "yt_dl",
    cancel_callback: str = "folder_cancel",
) -> InlineKeyboardMarkup:
    """Builds interactive track buttons from search results."""
    keyboard: List[List[InlineKeyboardButton]] = []
    for idx, item in enumerate(results):
        title = item.get("title", "Unknown Title")
        btn_text = f"▶️ {title[:40]}"
        keyboard.append(
            [InlineKeyboardButton(btn_text, callback_data=f"{prefix}:{idx}")]
        )
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=cancel_callback)])
    return InlineKeyboardMarkup(keyboard)


def build_metadata_review_keyboard(
    candidates: List[Any],
    session_id: str,
) -> InlineKeyboardMarkup:
    """Builds interactive inline keyboard for post-download metadata candidate review."""
    keyboard: List[List[InlineKeyboardButton]] = []
    source_icons = {
        "iTunes": "🍎",
        "MusicBrainz": "💿",
        "Deezer": "🎧",
        "Spotify": "🟢",
        "Discogs": "📀",
    }

    # Find recommended candidate index
    rec_idx = -1
    for idx, cand in enumerate(candidates):
        if getattr(cand, "is_recommended", False):
            rec_idx = idx
            break
    if rec_idx == -1 and candidates:
        rec_idx = 0

    # Quick action row: [✅ Accept Recommendation]
    if rec_idx >= 0 and rec_idx < len(candidates):
        rec_cand = candidates[rec_idx]
        rec_source = getattr(rec_cand, "source", "Best Match")
        rec_conf = int(getattr(rec_cand, "confidence_score", 0))
        keyboard.append([
            InlineKeyboardButton(
                f"✅ Accept Recommendation ({rec_source} {rec_conf}%)",
                callback_data=f"dlmeta_rec:{session_id}",
            )
        ])

    # Candidate source buttons: [Source Name (Confidence %)]
    cand_buttons: List[InlineKeyboardButton] = []
    for idx, cand in enumerate(candidates[:6]):
        source = getattr(cand, "source", "Provider")
        conf = int(getattr(cand, "confidence_score", 0))
        icon = source_icons.get(source, "🌐")
        star = "⭐ " if getattr(cand, "is_recommended", False) else ""
        btn_text = f"{star}{icon} {source} ({conf}%)"
        cand_buttons.append(
            InlineKeyboardButton(btn_text, callback_data=f"dlmeta_sel:{session_id}:{idx}")
        )

    # Group candidate buttons in pairs
    for i in range(0, len(cand_buttons), 2):
        keyboard.append(cand_buttons[i : i + 2])

    # Option to view tag diff / preview details before applying
    keyboard.append([
        InlineKeyboardButton("🔍 View Tag Diff / Preview", callback_data=f"dlmeta_diff:{session_id}:0")
    ])

    # Manual Override / Skip Tagging / Cancel
    keyboard.append([
        InlineKeyboardButton("⏩ Skip Tagging & Move", callback_data=f"dlmeta_skip:{session_id}"),
        InlineKeyboardButton("❌ Discard", callback_data=f"dlmeta_cancel:{session_id}"),
    ])

    return InlineKeyboardMarkup(keyboard)


def build_metadata_diff_keyboard(
    session_id: str,
    current_idx: int,
    total_candidates: int,
) -> InlineKeyboardMarkup:
    """Builds navigation and action keyboard for tag diff preview dialog."""
    keyboard: List[List[InlineKeyboardButton]] = []

    # Direct apply button for current candidate
    keyboard.append([
        InlineKeyboardButton("✅ Apply This Source", callback_data=f"dlmeta_sel:{session_id}:{current_idx}")
    ])

    # Navigation between candidates
    nav_row: List[InlineKeyboardButton] = []
    if total_candidates > 1:
        if current_idx > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"dlmeta_diff:{session_id}:{current_idx - 1}"))
        nav_row.append(InlineKeyboardButton(f"{current_idx + 1}/{total_candidates}", callback_data="noop"))
        if current_idx < total_candidates - 1:
            nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"dlmeta_diff:{session_id}:{current_idx + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    # Back to selection / Skip
    keyboard.append([
        InlineKeyboardButton("⬅️ Back to Review", callback_data=f"dlmeta_back:{session_id}"),
        InlineKeyboardButton("⏩ Skip Tagging", callback_data=f"dlmeta_skip:{session_id}"),
    ])

    return InlineKeyboardMarkup(keyboard)


def build_metadata_empty_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Builds fallback keyboard when no online metadata candidates could be resolved."""
    keyboard = [
        [InlineKeyboardButton("✅ Use Probed Tags & Move", callback_data=f"dlmeta_default:{session_id}")],
        [InlineKeyboardButton("⏩ Skip Tagging & Move", callback_data=f"dlmeta_skip:{session_id}")],
        [InlineKeyboardButton("❌ Discard Download", callback_data=f"dlmeta_cancel:{session_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)

