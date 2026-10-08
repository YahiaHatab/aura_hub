"""Integration and smoke tests for Telegram bot handler routers and application builder."""

import unittest
from telegram.ext import CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler

from handlers import common, download, library, metadata, navidrome, nowplaying, request, settings, system, users
from main import build_application


class TestHandlersAndApplication(unittest.TestCase):
    def test_routers_contain_handlers(self):
        self.assertGreater(len(common.router), 0)
        self.assertGreater(len(download.router), 0)
        self.assertGreater(len(library.router), 0)
        self.assertGreater(len(metadata.router), 0)
        self.assertGreater(len(navidrome.router), 0)
        self.assertGreater(len(nowplaying.router), 0)
        self.assertGreater(len(request.router), 0)
        self.assertGreater(len(settings.router), 0)
        self.assertGreater(len(system.router), 0)
        self.assertGreater(len(users.router), 0)

    def test_build_application(self):
        app = build_application()
        self.assertIsNotNone(app)

        # Collect registered command names across all handler groups
        registered_commands = set()
        has_callback_handler = False
        has_message_handler = False
        has_conversation_handler = False

        for group_handlers in app.handlers.values():
            for handler in group_handlers:
                if isinstance(handler, CommandHandler):
                    registered_commands.update(handler.commands)
                elif isinstance(handler, CallbackQueryHandler):
                    has_callback_handler = True
                elif isinstance(handler, MessageHandler):
                    has_message_handler = True
                elif isinstance(handler, ConversationHandler):
                    has_conversation_handler = True
                    for entry in handler.entry_points:
                        if isinstance(entry, CommandHandler):
                            registered_commands.update(entry.commands)

        expected_commands = {
            "start",
            "help",
            "hub",
            "status",
            "download",
            "genius",
            "search",
            "metadata",
            "meta",
            "retag",
            "delete",
            "remove",
            "rescan",
            "scanstatus",
            "users",
            "adduser",
            "request",
            "requests",
            "quality",
            "nowplaying",
            "np",
            "storage",
            "disk",
        }

        for cmd in expected_commands:
            self.assertIn(cmd, registered_commands, f"Command /{cmd} not registered in application")

        self.assertTrue(has_callback_handler, "No CallbackQueryHandlers registered")
        self.assertTrue(has_message_handler, "No MessageHandlers registered (auto link catcher missing)")
        self.assertTrue(has_conversation_handler, "No ConversationHandler registered (add user conversation missing)")

    def test_post_download_metadata_review_keyboards(self):
        from services.metadata import MetadataCandidate, UnifiedAlbumMetadata
        from utils.keyboards import (
            build_metadata_diff_keyboard,
            build_metadata_empty_keyboard,
            build_metadata_review_keyboard,
        )

        cand1 = MetadataCandidate(
            source="iTunes",
            confidence_score=98.0,
            is_recommended=True,
            album_data=UnifiedAlbumMetadata(album="Saharna Ya Leil", artist="Elissa"),
            preview={"has_cover": True, "has_lyrics": True, "track_count": 16},
        )
        cand2 = MetadataCandidate(
            source="MusicBrainz",
            confidence_score=85.0,
            is_recommended=False,
            album_data=UnifiedAlbumMetadata(album="Saharna Ya Leil", artist="Elissa"),
            preview={"has_cover": False, "has_lyrics": False, "track_count": 16},
        )

        # 1. Review keyboard with recommended option
        markup = build_metadata_review_keyboard([cand1, cand2], session_id="test_sess_1")
        callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertIn("dlmeta_rec:test_sess_1", callbacks)
        self.assertIn("dlmeta_sel:test_sess_1:0", callbacks)
        self.assertIn("dlmeta_sel:test_sess_1:1", callbacks)
        self.assertIn("dlmeta_diff:test_sess_1:0", callbacks)
        self.assertIn("dlmeta_skip:test_sess_1", callbacks)
        self.assertIn("dlmeta_cancel:test_sess_1", callbacks)

        # 2. Diff keyboard navigation
        diff_markup = build_metadata_diff_keyboard(session_id="test_sess_1", current_idx=0, total_candidates=2)
        diff_callbacks = [btn.callback_data for row in diff_markup.inline_keyboard for btn in row]
        self.assertIn("dlmeta_sel:test_sess_1:0", diff_callbacks)
        self.assertIn("dlmeta_diff:test_sess_1:1", diff_callbacks)
        self.assertIn("dlmeta_back:test_sess_1", diff_callbacks)

        # 3. Empty keyboard fallback
        empty_markup = build_metadata_empty_keyboard(session_id="test_sess_1")
        empty_callbacks = [btn.callback_data for row in empty_markup.inline_keyboard for btn in row]
        self.assertIn("dlmeta_default:test_sess_1", empty_callbacks)
        self.assertIn("dlmeta_skip:test_sess_1", empty_callbacks)
        self.assertIn("dlmeta_cancel:test_sess_1", empty_callbacks)

    def test_post_download_session_cancel_cleanup(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock
        from handlers.download import _DOWNLOAD_SESSIONS, download_metadata_callback_handler

        with tempfile.TemporaryDirectory() as tmp_dir:
            stg = Path(tmp_dir) / ".staging" / "dl_dummy"
            stg.mkdir(parents=True)
            dummy_file = stg / "test.mp3"
            dummy_file.write_bytes(b"dummy")

            sess_id = "test_cancel_sess"
            _DOWNLOAD_SESSIONS[sess_id] = {
                "session_id": sess_id,
                "staging_dir": stg,
                "detected_artist": "Artist",
                "detected_album": "Album",
                "candidates": [],
            }

            mock_query = MagicMock()
            mock_query.data = f"dlmeta_cancel:{sess_id}"
            mock_query.answer = AsyncMock()
            mock_query.edit_message_text = AsyncMock()

            mock_update = MagicMock()
            mock_update.callback_query = mock_query

            import asyncio
            asyncio.run(download_metadata_callback_handler(mock_update, MagicMock()))

            # Staging folder should be deleted and session removed
            self.assertFalse(stg.exists())
            self.assertNotIn(sess_id, _DOWNLOAD_SESSIONS)
            mock_query.edit_message_text.assert_called_once()


if __name__ == "__main__":
    unittest.main()

