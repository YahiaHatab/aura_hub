"""Settings handlers for Aura Hub: /quality command and interactive quality selector."""

import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from handlers.common import auth_required, is_admin
from services.settings import (
    QUALITY_LABELS,
    VALID_QUALITIES,
    get_quality_preference,
    set_quality_preference,
)

logger = logging.getLogger(__name__)


def build_quality_keyboard(current_qual: str) -> InlineKeyboardMarkup:
    """Builds inline keyboard for selecting audio download quality."""
    buttons = [
        ("auto", "💎 Auto (Lossless FLAC -> Opus Fallback)"),
        ("opus", "🎧 Pure Opus (Fast & Native 160k)"),
        ("mp3", "🎵 Pure MP3 (Standard 320k)"),
    ]

    keyboard = []
    for q_key, q_label in buttons:
        prefix = "✓ " if q_key == current_qual else ""
        keyboard.append(
            [InlineKeyboardButton(f"{prefix}{q_label}", callback_data=f"set_qual:{q_key}")]
        )

    return InlineKeyboardMarkup(keyboard)


@auth_required
async def quality_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /quality command: displays current setting and inline selection buttons."""
    if not update.message:
        return

    user = update.effective_user
    user_id = user.id if user else None
    current = get_quality_preference(user_id)
    label = QUALITY_LABELS.get(current, current.upper())

    text = (
        "⚙️ *Audio Download Quality Settings*\n\n"
        f"• *Current Preference:* `{label}`\n\n"
        "Select your preferred audio extraction format below.\n"
        "_Tip: You can also use temporary flags in `/download` (e.g. `--flac`, `--opus`, `--mp3`)._"
    )

    await update.message.reply_text(
        text,
        reply_markup=build_quality_keyboard(current),
        parse_mode="Markdown",
    )


@auth_required
async def quality_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles inline button tap to update quality preference."""
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    if not data.startswith("set_qual:"):
        return

    target_qual = data.split(":", 1)[1]
    if target_qual not in VALID_QUALITIES:
        await query.answer("Invalid quality option.", show_alert=True)
        return

    user = update.effective_user
    user_id = user.id if user else None
    admin_status = is_admin(user_id) if user_id else False

    # Persist choice for user (and global default if admin)
    saved_qual = set_quality_preference(
        target_qual, user_id=user_id, set_global=admin_status
    )
    label = QUALITY_LABELS.get(saved_qual, saved_qual.upper())

    await query.answer(f"Quality updated to: {label}")

    scope_note = " (Global Default)" if admin_status else " (Personal Preference)"
    updated_text = (
        "⚙️ *Audio Download Quality Settings*\n\n"
        f"✅ *Active Preference Updated:*\n`{label}`{scope_note}\n\n"
        "Future downloads and requests will respect this setting.\n"
        "_Tip: Override per download with `--flac`, `--opus`, or `--mp3`._"
    )

    try:
        await query.edit_message_text(
            updated_text,
            reply_markup=build_quality_keyboard(saved_qual),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.debug(f"Could not edit quality message: {e}")


router = [
    CommandHandler("quality", quality_handler),
    CallbackQueryHandler(quality_callback_handler, pattern=r"^set_qual:"),
]
