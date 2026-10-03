"""Navidrome user management handlers: /users and /adduser.

Allows listing, creating, editing (username and password), and deleting Subsonic user accounts
via interactive Telegram menus. Restricted to authorized administrators.
"""

import logging
import re
import secrets
from typing import Any, Dict, List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
from handlers.common import admin_required, is_admin
from services.navidrome import navidrome_client

logger = logging.getLogger(__name__)

# Conversation states
WAITING_ADD_USER_INPUT = 1
WAITING_NEW_USERNAME = 2
WAITING_NEW_PASSWORD = 3


def _format_user_role_badges(user: Dict[str, Any]) -> str:
    """Formats a concise string of role badges for a Navidrome user account."""
    roles: List[str] = []
    if user.get("adminRole"):
        roles.append("👑 Admin")
    if user.get("streamRole"):
        roles.append("🎧 Stream")
    if user.get("downloadRole"):
        roles.append("📥 Download")
    return f"[{', '.join(roles)}]" if roles else "[No Roles]"


def _build_users_list_view(users: List[Dict[str, Any]]) -> tuple[str, InlineKeyboardMarkup]:
    """Constructs the markdown text and inline keyboard for the accounts list."""
    total_users = len(users)
    text_lines = [
        f"👥 *Navidrome User Accounts* ({total_users} registered)\n",
    ]

    keyboard: List[List[InlineKeyboardButton]] = []

    for u in users:
        username = u.get("username", "Unknown")
        role_badges = _format_user_role_badges(u)
        email = u.get("email")
        email_str = f"\n   📧 `{email}`" if email else ""
        text_lines.append(f"• *{username}* {role_badges}{email_str}")

        # Individual management button for each user
        btn_text = f"👤 {username} {role_badges}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"u_manage:{username}")])

    text_lines.append("\n_Select a user above to manage, or tap 'Add User':_")
    text = "\n".join(text_lines)

    # Action buttons
    keyboard.append([
        InlineKeyboardButton("➕ Add User", callback_data="u_add"),
        InlineKeyboardButton("🔄 Refresh", callback_data="u_refresh"),
    ])
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="u_close")])

    return text, InlineKeyboardMarkup(keyboard)


@admin_required
async def users_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /users command. Lists Navidrome accounts with interactive buttons."""
    if not update.effective_message:
        return

    if not navidrome_client.is_configured():
        await update.effective_message.reply_markdown(
            "⚠️ *Navidrome is not configured.*\n\n"
            "Please configure `NAVIDROME_USER` and `NAVIDROME_PASS` in `config.py` or `.env`."
        )
        return

    res = navidrome_client.get_users()
    if not res.get("ok"):
        err = res.get("message", "Unknown error")
        await update.effective_message.reply_markdown(f"❌ *Failed to fetch users:*\n`{err}`")
        return

    users = res.get("users", [])
    text, reply_markup = _build_users_list_view(users)
    await update.effective_message.reply_markdown(text, reply_markup=reply_markup)


async def user_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Refreshes or navigates back to the main users list."""
    query = update.callback_query
    if not query or not query.from_user or not is_admin(query.from_user.id):
        if query:
            await query.answer("⛔ Admin privileges required.", show_alert=True)
        return
    await query.answer()

    res = navidrome_client.get_users()
    if not res.get("ok"):
        err = res.get("message", "Unknown error")
        await query.edit_message_text(f"❌ *Failed to fetch users:*\n`{err}`", parse_mode="Markdown")
        return

    users = res.get("users", [])
    text, reply_markup = _build_users_list_view(users)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def user_manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the action card for an individual Navidrome user account."""
    query = update.callback_query
    if not query or not query.from_user or not is_admin(query.from_user.id):
        if query:
            await query.answer("⛔ Admin privileges required.", show_alert=True)
        return
    await query.answer()

    data = query.data or ""
    username = data.split(":", 1)[1] if ":" in data else ""
    if not username:
        await query.edit_message_text("⚠️ User not specified.")
        return

    res = navidrome_client.get_user(username)
    user_data = res.get("user", {}) if res.get("ok") else {}
    email = user_data.get("email") or "Not configured"
    admin_str = "✅ Yes" if user_data.get("adminRole") else "❌ No"
    stream_str = "✅ Yes" if user_data.get("streamRole") else "❌ No"
    download_str = "✅ Yes" if user_data.get("downloadRole") else "❌ No"

    text = (
        f"👤 *Navidrome Account:* `{username}`\n\n"
        f"• *Email:* `{email}`\n"
        f"• *Admin Privileges:* {admin_str}\n"
        f"• *Audio Streaming:* {stream_str}\n"
        f"• *Track Downloads:* {download_str}\n\n"
        "_Choose an action below:_"
    )

    keyboard = [
        [InlineKeyboardButton("✏️ Edit User", callback_data=f"u_edit_menu:{username}")],
        [InlineKeyboardButton("🗑️ Delete User", callback_data=f"u_del:{username}")],
        [InlineKeyboardButton("⬅️ Back to Users", callback_data="u_list")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def user_edit_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Presents the edit options menu: Change Username or Change Password."""
    query = update.callback_query
    if not query or not query.from_user or not is_admin(query.from_user.id):
        if query:
            await query.answer("⛔ Admin privileges required.", show_alert=True)
        return
    await query.answer()

    data = query.data or ""
    username = data.split(":", 1)[1] if ":" in data else ""
    if not username:
        return

    text = (
        f"✏️ *Edit Account:* `{username}`\n\n"
        "What would you like to update?\n"
        "• *Username:* Rename the user login ID\n"
        "• *Password:* Set a custom or auto-generated password"
    )

    keyboard = [
        [InlineKeyboardButton("👤 Change Username", callback_data=f"u_edit_name:{username}")],
        [InlineKeyboardButton("🔑 Change Password", callback_data=f"u_edit_pass:{username}")],
        [InlineKeyboardButton("⬅️ Back to User", callback_data=f"u_manage:{username}")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def user_delete_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Presents a confirmation dialog before permanently removing an account."""
    query = update.callback_query
    if not query or not query.from_user or not is_admin(query.from_user.id):
        if query:
            await query.answer("⛔ Admin privileges required.", show_alert=True)
        return
    await query.answer()

    data = query.data or ""
    username = data.split(":", 1)[1] if ":" in data else ""
    if not username:
        return

    # Protection against deleting primary configured admin
    if config.NAVIDROME_USER and username.lower() == config.NAVIDROME_USER.lower():
        text = (
            f"⚠️ *Action Forbidden*\n\n"
            f"Cannot delete `{username}` because it is the primary server admin "
            "configured in `config.py`."
        )
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data=f"u_manage:{username}")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    text = (
        f"⚠️ *Confirm Account Deletion*\n\n"
        f"Are you sure you want to permanently delete user `{username}` from Navidrome?\n\n"
        "_This user will immediately lose access to all streaming and downloads._"
    )
    keyboard = [
        [InlineKeyboardButton(f"✅ Yes, Delete {username}", callback_data=f"u_del_confirm:{username}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"u_manage:{username}")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def user_delete_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executes the deleteUser API call upon user confirmation."""
    query = update.callback_query
    if not query or not query.from_user or not is_admin(query.from_user.id):
        if query:
            await query.answer("⛔ Admin privileges required.", show_alert=True)
        return
    await query.answer()

    data = query.data or ""
    username = data.split(":", 1)[1] if ":" in data else ""
    if not username:
        return

    res = navidrome_client.delete_user(username)
    if res.get("ok"):
        text = f"🗑️ *User Deleted:*\nAccount `{username}` has been permanently removed from Navidrome."
    else:
        err = res.get("message", "Unknown error")
        text = f"❌ *Failed to delete user `{username}`:*\n`{err}`"

    keyboard = [[InlineKeyboardButton("⬅️ Back to Users", callback_data="u_list")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def user_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dismisses the user management card."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    await query.edit_message_text("❌ User management panel closed.")


# ================================================================
# CONVERSATION HANDLERS: ADD USER, EDIT USERNAME, EDIT PASSWORD
# ================================================================

# --- ADD USER ---
async def start_add_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the add-user conversation from an inline button."""
    query = update.callback_query
    if not query or not query.from_user or not is_admin(query.from_user.id):
        if query:
            await query.answer("⛔ Admin privileges required.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    prompt_text = (
        "➕ *Create New Navidrome User*\n\n"
        "Send the new account details in this format:\n"
        "`<username> [optional_password]`\n\n"
        "*Examples:*\n"
        "• `sarah` _(generates a secure random password automatically)_\n"
        "• `sarah SecretPass123`\n\n"
        "Send `/cancel` to abort."
    )
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="u_cancel_conv")]]
    await query.edit_message_text(
        prompt_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return WAITING_ADD_USER_INPUT


@admin_required
async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the add-user conversation from /adduser command."""
    if not update.effective_message:
        return ConversationHandler.END

    if not navidrome_client.is_configured():
        await update.effective_message.reply_markdown(
            "⚠️ *Navidrome is not configured.*\n\n"
            "Please configure `NAVIDROME_USER` and `NAVIDROME_PASS` in `config.py` or `.env`."
        )
        return ConversationHandler.END

    prompt_text = (
        "➕ *Create New Navidrome User*\n\n"
        "Send the new account details in this format:\n"
        "`<username> [optional_password]`\n\n"
        "*Examples:*\n"
        "• `sarah` _(generates a secure random password automatically)_\n"
        "• `sarah SecretPass123`\n\n"
        "Send `/cancel` to abort."
    )
    await update.effective_message.reply_markdown(prompt_text)
    return WAITING_ADD_USER_INPUT


async def process_add_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Processes user input, calls createUser API, and responds with credentials."""
    user = update.effective_user
    if not user or not is_admin(user.id) or not update.message or not update.message.text:
        return ConversationHandler.END

    text = update.message.text.strip()
    parts = text.split()
    if not parts:
        await update.message.reply_text("Please provide at least a username. Try again or type /cancel:")
        return WAITING_ADD_USER_INPUT

    username = parts[0].strip()
    password = parts[1].strip() if len(parts) > 1 else secrets.token_urlsafe(10)

    # Basic username sanitization check
    if not re.match(r"^[a-zA-Z0-9_.\-]+$", username) or len(username) < 2:
        await update.message.reply_text(
            "⚠️ Invalid username. Usernames must be at least 2 characters and contain "
            "only letters, numbers, dots, hyphens, or underscores. Try again or /cancel:"
        )
        return WAITING_ADD_USER_INPUT

    status_msg = await update.message.reply_text("⏳ Creating account on Navidrome server...")

    res = navidrome_client.create_user(
        username=username,
        password=password,
        admin_role=False,
        stream_role=True,
        download_role=True,
    )

    if res.get("ok"):
        reply_text = (
            f"🎉 *User Created Successfully!*\n\n"
            f"• *Username:* `{username}`\n"
            f"• *Password:* `{password}`\n"
            f"• *Permissions:* `🎧 Stream, 📥 Download`\n\n"
            "✨ _The account is ready to log into Navidrome, Symfonium, DSub, etc._"
        )
    else:
        err = res.get("message", "Unknown error")
        reply_text = f"❌ *Failed to create user `{username}`:*\n`{err}`"

    keyboard = [[InlineKeyboardButton("👥 View All Users", callback_data="u_list")]]
    await status_msg.edit_text(reply_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ConversationHandler.END


# --- EDIT USERNAME ---
async def start_edit_username_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompts for a new username."""
    query = update.callback_query
    if not query or not query.from_user or not is_admin(query.from_user.id):
        if query:
            await query.answer("⛔ Admin privileges required.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    data = query.data or ""
    username = data.split(":", 1)[1] if ":" in data else ""
    if not username:
        return ConversationHandler.END

    if config.NAVIDROME_USER and username.lower() == config.NAVIDROME_USER.lower():
        await query.edit_message_text(
            f"⚠️ Cannot rename the primary server admin account (`{username}`) configured in `config.py`.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"u_manage:{username}")]]),
        )
        return ConversationHandler.END

    context.user_data["edit_target_user"] = username

    text = (
        f"👤 *Change Username for:* `{username}`\n\n"
        "Send the new username in chat.\n"
        "_(Letters, numbers, underscores, hyphens; min 2 characters)_\n\n"
        "Send `/cancel` to abort."
    )
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="u_cancel_conv")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return WAITING_NEW_USERNAME


async def process_new_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Updates the username in Navidrome."""
    user = update.effective_user
    if not user or not is_admin(user.id) or not update.message or not update.message.text:
        return ConversationHandler.END

    target_user = context.user_data.get("edit_target_user")
    if not target_user:
        await update.message.reply_text("⚠️ Session expired. Please start over with `/users`.")
        return ConversationHandler.END

    new_username = update.message.text.strip()
    if not re.match(r"^[a-zA-Z0-9_.\-]+$", new_username) or len(new_username) < 2:
        await update.message.reply_text(
            "⚠️ Invalid username. Must be at least 2 characters (letters, numbers, hyphens, underscores). Try again or /cancel:"
        )
        return WAITING_NEW_USERNAME

    res = navidrome_client.update_user(target_user, new_username=new_username)
    if res.get("ok"):
        reply_text = (
            f"✅ *Username Updated Successfully!*\n\n"
            f"• *Previous Username:* `{target_user}`\n"
            f"• *New Username:* `{new_username}`\n\n"
            "_The account name has been updated in Navidrome immediately._"
        )
        keyboard = [
            [InlineKeyboardButton("👤 View User", callback_data=f"u_manage:{new_username}")],
            [InlineKeyboardButton("👥 All Users", callback_data="u_list")],
        ]
    else:
        err = res.get("message", "Unknown error")
        reply_text = f"❌ *Failed to update username:*\n`{err}`"
        keyboard = [[InlineKeyboardButton("⬅️ Back to Users", callback_data="u_list")]]

    await update.message.reply_markdown(reply_text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END


# --- EDIT PASSWORD ---
async def start_edit_password_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Presents options to auto-generate or type a custom password."""
    query = update.callback_query
    if not query or not query.from_user or not is_admin(query.from_user.id):
        if query:
            await query.answer("⛔ Admin privileges required.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    data = query.data or ""
    username = data.split(":", 1)[1] if ":" in data else ""
    if not username:
        return ConversationHandler.END

    context.user_data["edit_target_user"] = username

    text = (
        f"🔑 *Change Password for:* `{username}`\n\n"
        "• *Type your custom password* directly in chat (minimum 4 characters)\n"
        "• Or tap *🎲 Auto-Generate* below for an instant secure password.\n\n"
        "Send `/cancel` to abort."
    )
    keyboard = [
        [InlineKeyboardButton("🎲 Auto-Generate Password", callback_data=f"u_pass_auto:{username}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="u_cancel_conv")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return WAITING_NEW_PASSWORD


async def auto_generate_password_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Instantly generates and assigns a secure random password."""
    query = update.callback_query
    if not query or not query.from_user or not is_admin(query.from_user.id):
        if query:
            await query.answer("⛔ Admin privileges required.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    data = query.data or ""
    username = data.split(":", 1)[1] if ":" in data else ""
    if not username:
        return ConversationHandler.END

    new_password = secrets.token_urlsafe(10)
    res = navidrome_client.update_user(username=username, password=new_password)

    if res.get("ok"):
        text = (
            f"🔑 *Password Updated Successfully!*\n\n"
            f"• *Username:* `{username}`\n"
            f"• *New Password:* `{new_password}`\n\n"
            "⚠️ _Please copy and share this password with the user immediately._"
        )
    else:
        err = res.get("message", "Unknown error")
        text = f"❌ *Failed to update password for `{username}`:*\n`{err}`"

    keyboard = [
        [InlineKeyboardButton("👤 View User", callback_data=f"u_manage:{username}")],
        [InlineKeyboardButton("👥 All Users", callback_data="u_list")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ConversationHandler.END


async def process_new_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Applies a custom typed password to the Navidrome account."""
    user = update.effective_user
    if not user or not is_admin(user.id) or not update.message or not update.message.text:
        return ConversationHandler.END

    target_user = context.user_data.get("edit_target_user")
    if not target_user:
        await update.message.reply_text("⚠️ Session expired. Please start over with `/users`.")
        return ConversationHandler.END

    new_password = update.message.text.strip()
    if len(new_password) < 4:
        await update.message.reply_text("⚠️ Password must be at least 4 characters. Try again or /cancel:")
        return WAITING_NEW_PASSWORD

    res = navidrome_client.update_user(username=target_user, password=new_password)
    if res.get("ok"):
        reply_text = (
            f"🔑 *Password Updated Successfully!*\n\n"
            f"• *Username:* `{target_user}`\n"
            f"• *New Password:* `{new_password}`\n\n"
            "✨ _The new password is now active in Navidrome immediately._"
        )
        keyboard = [
            [InlineKeyboardButton("👤 View User", callback_data=f"u_manage:{target_user}")],
            [InlineKeyboardButton("👥 All Users", callback_data="u_list")],
        ]
    else:
        err = res.get("message", "Unknown error")
        reply_text = f"❌ *Failed to update password:*\n`{err}`"
        keyboard = [[InlineKeyboardButton("⬅️ Back to Users", callback_data="u_list")]]

    await update.message.reply_markdown(reply_text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END


async def cancel_user_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels any ongoing user edit/add conversation."""
    target_user = context.user_data.get("edit_target_user")
    keyboard = []
    if target_user:
        keyboard.append([InlineKeyboardButton("⬅️ Back to User", callback_data=f"u_manage:{target_user}")])
    else:
        keyboard.append([InlineKeyboardButton("👥 All Users", callback_data="u_list")])

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "❌ Action cancelled.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            "❌ Action cancelled.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    return ConversationHandler.END


# Unified Conversation handler for add/edit operations
user_conversation = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(start_add_user_callback, pattern=r"^u_add$"),
        CommandHandler("adduser", add_user_command),
        CallbackQueryHandler(start_edit_username_callback, pattern=r"^u_edit_name:"),
        CallbackQueryHandler(start_edit_password_callback, pattern=r"^u_edit_pass:"),
    ],
    states={
        WAITING_ADD_USER_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, process_add_user_input),
        ],
        WAITING_NEW_USERNAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, process_new_username),
        ],
        WAITING_NEW_PASSWORD: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, process_new_password),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_user_conv),
        CallbackQueryHandler(cancel_user_conv, pattern=r"^u_cancel_conv$"),
        CallbackQueryHandler(auto_generate_password_callback, pattern=r"^u_pass_auto:"),
    ],
    allow_reentry=True,
    per_message=False,
)

# Exported router for main.py registration
router = [
    user_conversation,
    CommandHandler("users", users_command_handler),
    CallbackQueryHandler(user_list_callback, pattern=r"^(u_list|u_refresh)$"),
    CallbackQueryHandler(user_manage_callback, pattern=r"^u_manage:"),
    CallbackQueryHandler(user_edit_menu_callback, pattern=r"^u_edit_menu:"),
    CallbackQueryHandler(user_delete_prompt_callback, pattern=r"^u_del:"),
    CallbackQueryHandler(user_delete_confirm_callback, pattern=r"^u_del_confirm:"),
    CallbackQueryHandler(user_close_callback, pattern=r"^u_close$"),
]
