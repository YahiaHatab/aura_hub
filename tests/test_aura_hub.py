"""Comprehensive test suite for Aura Hub modular components."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from config import PAGE_SIZE
from services.navidrome import NavidromeClient
from services.system import delete_album_folder, get_album_folders, get_disk_metrics
from utils.helpers import (
    extract_clean_artists,
    format_bytes,
    franco_to_arabic,
    get_clean_name,
    parse_genius_input,
    sanitize_filename,
)
from utils.keyboards import (
    build_confirmation_keyboard,
    build_folder_keyboard,
    build_search_results_keyboard,
)


class TestHelpers(unittest.TestCase):
    def test_get_clean_name(self):
        self.assertEqual(get_clean_name("01 - Test Song (Remix)"), "test song remix")
        self.assertEqual(get_clean_name("02. Artist - Track [HQ]"), "artist track hq")

    def test_extract_clean_artists(self):
        # Bilingual combined string
        candidates = extract_clean_artists("Mohamed Mounir  محمد منير")
        self.assertIn("Mohamed Mounir", candidates)
        self.assertIn("محمد منير", candidates)

        # Single Latin artist
        candidates_latin = extract_clean_artists("Amr Diab")
        self.assertIn("Amr Diab", candidates_latin)

        # Generic artist names ignored
        self.assertEqual(extract_clean_artists("Unknown Artist"), [])
        self.assertEqual(extract_clean_artists("Various"), [])

    def test_franco_to_arabic(self):
        variants = franco_to_arabic("banat 3aks el donya")
        # Should convert 'banat' -> بنات, '3aks' -> عكس, 'el' -> ال
        arabic_found = any("بنات" in v and "عكس" in v and "ال" in v for v in variants)
        self.assertTrue(arabic_found, f"Arabic translation not found in variants: {variants}")

        # Test single number replacements
        self.assertTrue(any("حبيبي" in v for v in franco_to_arabic("7abibi")))

    def test_parse_genius_input(self):
        url = "https://genius.com/albums/Amr-diab/Kol-hayaty-lyrics"
        parsed = parse_genius_input(url)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.get("artist"), "Amr diab")
        self.assertEqual(parsed.get("album"), "Kol hayaty")

        id_url = "https://genius.com/albums/12345"
        parsed_id = parse_genius_input(id_url)
        self.assertIsNotNone(parsed_id)
        self.assertEqual(parsed_id.get("album_id"), 12345)

        self.assertIsNone(parse_genius_input("plain text"))

    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename("Album: The Best / Tracks?"), "Album The Best Tracks")
        self.assertEqual(sanitize_filename("Valid_Name-123"), "Valid_Name-123")

    def test_format_bytes(self):
        self.assertEqual(format_bytes(1024), "1.0 KB")
        self.assertEqual(format_bytes(1024 * 1024 * 1024), "1.0 GB")


class TestKeyboards(unittest.TestCase):
    def test_build_folder_keyboard_pagination(self):
        folders = [f"Artist/Album_{i}" for i in range(15)]
        markup_page0 = build_folder_keyboard(folders, prefix="retag", page=0, page_size=6)
        # Should have 6 items + 1 nav row + 1 cancel row = 8 rows
        self.assertEqual(len(markup_page0.inline_keyboard), 8)

        # Check navigation row
        nav_buttons = markup_page0.inline_keyboard[-2]
        self.assertTrue(any(btn.callback_data == "noop" and "1/3" in btn.text for btn in nav_buttons))
        self.assertTrue(any("Next" in btn.text and "retag_page:1" == btn.callback_data for btn in nav_buttons))

    def test_build_confirmation_keyboard(self):
        markup = build_confirmation_keyboard("del_confirm:2", "folder_cancel")
        self.assertEqual(len(markup.inline_keyboard), 2)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "del_confirm:2")
        self.assertEqual(markup.inline_keyboard[1][0].callback_data, "folder_cancel")

    def test_build_search_results_keyboard(self):
        results = [
            {"title": "Track 1", "url": "https://youtu.be/1"},
            {"title": "Track 2", "url": "https://youtu.be/2"},
        ]
        markup = build_search_results_keyboard(results, prefix="yt_dl")
        self.assertEqual(len(markup.inline_keyboard), 3)  # 2 results + 1 cancel
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "yt_dl:0")
        self.assertEqual(markup.inline_keyboard[1][0].callback_data, "yt_dl:1")


class TestSystemServices(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.test_dir.name)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_get_album_folders_and_disk_metrics(self):
        # Create dummy structure
        album_dir = self.base_path / "Artist One" / "Album One"
        album_dir.mkdir(parents=True)
        (album_dir / "01 - Song.mp3").write_text("dummy audio", encoding="utf-8")
        (album_dir / "01 - Song.lrc").write_text("[00:00.00] Lyrics", encoding="utf-8")

        folders = get_album_folders(self.base_path)
        self.assertEqual(len(folders), 1)
        self.assertEqual(folders[0], "Artist One/Album One")

        metrics = get_disk_metrics(self.base_path)
        self.assertEqual(metrics["mp3_count"], 1)
        self.assertEqual(metrics["lrc_count"], 1)
        self.assertGreater(metrics["total_gb"], 0)

    def test_delete_album_folder_security_and_cleanup(self):
        # Create dummy album and empty artist parent
        album_dir = self.base_path / "Artist One" / "Album To Delete"
        album_dir.mkdir(parents=True)
        (album_dir / "01 - Song.mp3").write_text("dummy audio", encoding="utf-8")

        # Test path traversal attack prevention
        success, msg = delete_album_folder("../../etc", self.base_path)
        self.assertFalse(success)
        self.assertIn("Security violation", msg)

        # Test valid deletion
        success, msg = delete_album_folder("Artist One/Album To Delete", self.base_path)
        self.assertTrue(success)
        self.assertFalse(album_dir.exists())
        # Artist One was empty, so parent directory should have been cleaned
        self.assertFalse((self.base_path / "Artist One").exists())


class TestNavidromeClient(unittest.TestCase):
    def test_auth_params_and_configuration(self):
        client = NavidromeClient(
            base_url="http://navidrome.local:4533",
            username="admin",
            password="secretpassword",
        )
        self.assertTrue(client.is_configured())

        params = client._build_auth_params()
        self.assertEqual(params["u"], "admin")
        self.assertEqual(params["v"], "1.16.1")
        self.assertEqual(params["c"], "AuraHub")
        self.assertEqual(params["f"], "json")

        salt = params["s"]
        expected_token = hashlib.md5(("secretpassword" + salt).encode("utf-8")).hexdigest()
        self.assertEqual(params["t"], expected_token)

    def test_unconfigured_client_graceful_response(self):
        client = NavidromeClient(base_url="http://navidrome.local", username="", password="")
        self.assertFalse(client.is_configured())
        res = client.start_scan()
        self.assertFalse(res["ok"])
        self.assertIn("not configured", res["message"])

        users_res = client.get_users()
        self.assertFalse(users_res["ok"])

    def test_user_management_methods(self):
        client = NavidromeClient(
            base_url="http://navidrome.local:4533",
            username="admin",
            password="secretpassword",
        )

        # 1. Native API get_users
        with patch.object(client, "_native_request", return_value=(True, [{"id": "u1", "userName": "Bonga", "isAdmin": False}])):
            res = client.get_users()
            self.assertTrue(res["ok"])
            self.assertEqual(len(res["users"]), 1)
            self.assertEqual(res["users"][0]["username"], "Bonga")

        # 2. Subsonic fallback get_users
        with patch.object(client, "_native_request", return_value=(False, "error")), \
             patch.object(client, "_request", return_value={"ok": True, "data": {"users": {"user": {"username": "admin", "adminRole": True}}}}):
            res = client.get_users()
            self.assertTrue(res["ok"])
            self.assertEqual(len(res["users"]), 1)
            self.assertEqual(res["users"][0]["username"], "admin")

        # 3. Native create_user
        with patch.object(client, "_native_request", return_value=(True, {"id": "new1"})) as mock_native:
            res = client.create_user("bob", "password123", email="bob@test.com", admin_role=False)
            self.assertTrue(res["ok"])
            mock_native.assert_called_once_with("POST", "user", {
                "userName": "bob",
                "name": "bob",
                "password": "password123",
                "email": "bob@test.com",
                "isAdmin": False,
            })

        # 4. Native update_user
        with patch.object(client, "get_user", return_value={"ok": True, "user": {"id": "uid123", "username": "bob"}}), \
             patch.object(client, "_native_request", return_value=(True, {"id": "uid123"})) as mock_native:
            res = client.update_user("bob", password="newpass123")
            self.assertTrue(res["ok"])
            mock_native.assert_called_once_with("PUT", "user/uid123", {"password": "newpass123"})

        # 5. Native delete_user
        with patch.object(client, "get_user", return_value={"ok": True, "user": {"id": "uid123", "username": "bob"}}), \
             patch.object(client, "_native_request", return_value=(True, {})) as mock_native:
            res = client.delete_user("bob")
            self.assertTrue(res["ok"])
            mock_native.assert_called_once_with("DELETE", "user/uid123")


class TestAuthAndRequestQueue(unittest.TestCase):
    def test_auth_checks(self):
        from handlers.common import is_admin, is_authorized
        with patch("config.ADMIN_USER_IDS", {111}), patch("config.ALLOWED_USER_IDS", {111, 222}):
            self.assertTrue(is_admin(111))
            self.assertFalse(is_admin(222))
            self.assertFalse(is_admin(333))

            self.assertTrue(is_authorized(111))
            self.assertTrue(is_authorized(222))
            self.assertFalse(is_authorized(333))

    def test_request_safe_md(self):
        from handlers.request import _safe_md
        self.assertEqual(_safe_md("Amr Diab `Tamally Maak`"), "Amr Diab 'Tamally Maak'")
        self.assertEqual(_safe_md("Standard Title"), "Standard Title")


if __name__ == "__main__":
    unittest.main()

