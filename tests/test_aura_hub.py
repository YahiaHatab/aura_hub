"""Comprehensive test suite for Aura Hub modular components."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from config import PAGE_SIZE
from services.metadata import (
    _ARTIST_ALIASES_CACHE,
    fetch_mb_artist_aliases,
    resolve_canonical_artist,
)
from services.navidrome import NavidromeClient
from services.system import (
    delete_album_folder,
    get_album_folders,
    get_disk_metrics,
    rehome_album_folder,
)
from services.tagger import tag_album_hybrid
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
        self.assertEqual(metrics["audio_count"], 1)
        self.assertEqual(metrics["mp3_count"], 1)
        self.assertEqual(metrics["lrc_count"], 1)
        self.assertGreater(metrics["total_gb"], 0)

    def test_universal_audio_extensions_indexing(self):
        # Create folders containing FLAC, Opus, M4A, and MP3
        flac_album = self.base_path / "Artist Flac" / "Flac Album"
        flac_album.mkdir(parents=True)
        (flac_album / "01 - Track.flac").write_text("flac", encoding="utf-8")

        opus_album = self.base_path / "Artist Opus" / "Opus Album"
        opus_album.mkdir(parents=True)
        (opus_album / "01 - Track.opus").write_text("opus", encoding="utf-8")

        m4a_album = self.base_path / "Artist M4A" / "M4A Album"
        m4a_album.mkdir(parents=True)
        (m4a_album / "01 - Track.m4a").write_text("m4a", encoding="utf-8")

        # Hidden folder should be ignored
        hidden_album = self.base_path / ".cache" / "Hidden Album"
        hidden_album.mkdir(parents=True)
        (hidden_album / "01 - Track.flac").write_text("hidden", encoding="utf-8")

        # Non-audio folder should be ignored
        txt_album = self.base_path / "Artist Docs" / "Docs Album"
        txt_album.mkdir(parents=True)
        (txt_album / "notes.txt").write_text("notes", encoding="utf-8")

        folders = get_album_folders(self.base_path)
        self.assertEqual(len(folders), 3)
        self.assertIn("Artist Flac/Flac Album", folders)
        self.assertIn("Artist Opus/Opus Album", folders)
        self.assertIn("Artist M4A/M4A Album", folders)

        metrics = get_disk_metrics(self.base_path)
        self.assertEqual(metrics["audio_count"], 3)

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


class TestNowPlaying(unittest.TestCase):
    def setUp(self):
        self.client = NavidromeClient("http://localhost:4533", "admin", "secret")

    def test_get_now_playing_idle(self):
        with patch.object(self.client, "_request", return_value={"ok": True, "data": {"nowPlaying": {}}}):
            res = self.client.get_now_playing()
            self.assertTrue(res["ok"])
            self.assertEqual(res["entries"], [])
            self.assertEqual(res["count"], 0)

    def test_get_now_playing_active_streams(self):
        sample_entry = {
            "id": "123",
            "title": "Hotel California",
            "artist": "Eagles",
            "album": "Hotel California",
            "username": "yahia",
            "playerName": "Symfonium",
            "bitRate": 320,
            "suffix": "flac",
            "minutesAgo": 2,
            "duration": 390,
        }
        with patch.object(self.client, "_request", return_value={"ok": True, "data": {"nowPlaying": {"entry": [sample_entry]}}}) as mock_req:
            res = self.client.get_now_playing()
            mock_req.assert_called_once_with("getNowPlaying", {"f": "json"})
            self.assertTrue(res["ok"])
            self.assertEqual(res["count"], 1)
            self.assertEqual(res["streams"], res["entries"])
            entry = res["entries"][0]
            self.assertEqual(entry["username"], "yahia")
            self.assertEqual(entry["player"], "Symfonium")
            self.assertEqual(entry["title"], "Hotel California")
            self.assertEqual(entry["artist"], "Eagles")
            self.assertEqual(entry["album"], "Hotel California")
            self.assertEqual(entry["bitrate"], 320)
            self.assertEqual(entry["bitRate"], 320)
            self.assertEqual(entry["format"], "FLAC")
            self.assertEqual(entry["minutes_ago"], 2)
            self.assertEqual(entry["minutesAgo"], 2)

    def test_get_now_playing_single_dict_quirk(self):
        """Subsonic sometimes serializes a single entry as a dict instead of a list."""
        sample_entry = {
            "id": "456",
            "title": "Shape of You",
            "artist": "Ed Sheeran",
            "album": "Divide",
            "username": "guest",
            "clientName": "Navidrome Web",
            "bitRate": 256,
            "suffix": "mp3",
            "minutesAgo": 0,
            "duration": 233,
        }
        with patch.object(self.client, "_request", return_value={"ok": True, "data": {"nowPlaying": {"entry": sample_entry}}}):
            res = self.client.get_now_playing()
            self.assertTrue(res["ok"])
            self.assertEqual(res["count"], 1)
            self.assertEqual(len(res["streams"]), 1)
            entry = res["streams"][0]
            self.assertEqual(entry["username"], "guest")
            self.assertEqual(entry["player"], "Navidrome Web")
            self.assertEqual(entry["title"], "Shape of You")
            self.assertEqual(entry["artist"], "Ed Sheeran")
            self.assertEqual(entry["album"], "Divide")
            self.assertEqual(entry["bitRate"], 256)
            self.assertEqual(entry["format"], "MP3")

    def test_build_now_playing_card(self):
        from handlers.nowplaying import build_now_playing_response

        # Idle state
        idle_card = build_now_playing_response({"ok": True, "entries": []})
        self.assertIn("Server is currently idle", idle_card)

        # Active state
        active_card = build_now_playing_response({
            "ok": True,
            "entries": [{
                "username": "yahia",
                "player": "Symfonium",
                "title": "Hotel California",
                "artist": "Eagles",
                "album": "Hotel California",
                "bitrate": 320,
                "format": "FLAC",
                "minutes_ago": 0,
                "duration": 390,
            }],
        })
        self.assertIn("yahia", active_card)
        self.assertIn("Symfonium", active_card)
        self.assertIn("Hotel California", active_card)
        self.assertIn("Eagles", active_card)
        self.assertIn("320 kbps • FLAC", active_card)


class TestCanonicalArtistAndLibraryUnification(unittest.TestCase):
    def setUp(self):
        _ARTIST_ALIASES_CACHE.clear()

    def test_resolve_canonical_artist_tier1_mb_alias(self):
        # Tier 1: MusicBrainz entity has Latin/English alias
        credits = [
            {
                "name": "عمرو دياب",
                "artist": {
                    "id": "mbid-amr",
                    "name": "عمرو دياب",
                    "aliases": [
                        {"name": "Diab, Amr", "locale": "en", "type": "Artist name"},
                        {"name": "Amr Diab", "locale": "en", "type": "Artist name", "primary": True},
                        {"name": "عمرو عبد الباسط عبد العزيز دياب", "locale": "ar", "type": "Legal name"},
                    ],
                },
            }
        ]
        # Should select "Amr Diab" over Arabic credit and inverted "Diab, Amr"
        resolved = resolve_canonical_artist(credits, fallback_artist="عمرو دياب")
        self.assertEqual(resolved, "Amr Diab")

    def test_resolve_canonical_artist_tier1_fetch_remote_if_needed(self):
        # Entity has MBID but aliases empty on credit; fetch via fetch_mb_artist_aliases
        credits = [
            {
                "name": "محمد منير",
                "artist": {
                    "id": "mbid-mounir",
                    "name": "محمد منير",
                    "aliases": [],
                },
            }
        ]
        mock_aliases = [{"name": "Mohamed Mounir", "locale": "en", "type": "Artist name", "primary": True}]
        with patch("services.metadata.fetch_mb_artist_aliases", return_value=mock_aliases):
            resolved = resolve_canonical_artist(credits, fallback_artist="محمد منير")
            self.assertEqual(resolved, "Mohamed Mounir")

    def test_resolve_canonical_artist_tier2_source_latin_candidate(self):
        # Tier 2: MusicBrainz has no Latin alias, but fallback_artist has Latin candidate
        credits = [
            {
                "name": "عمرو دياب",
                "artist": {
                    "id": "",
                    "name": "عمرو دياب",
                    "aliases": [],
                },
            }
        ]
        # Input provides Latin name
        resolved = resolve_canonical_artist(credits, fallback_artist="Amr Diab")
        self.assertEqual(resolved, "Amr Diab")

        # Bilingual input provides Latin name
        resolved_bilingual = resolve_canonical_artist(credits, fallback_artist="Mohamed Mounir  محمد منير")
        self.assertEqual(resolved_bilingual, "Mohamed Mounir")

    def test_resolve_canonical_artist_tier2_ascii_credit_fallback(self):
        # Credit name is already ASCII (e.g. Coldplay), aliases empty, input empty
        credits = [{"name": "Coldplay", "artist": {"id": "", "name": "Coldplay", "aliases": []}}]
        resolved = resolve_canonical_artist(credits, fallback_artist="")
        self.assertEqual(resolved, "Coldplay")

    def test_resolve_canonical_artist_tier3_native_script_fallback(self):
        # No Latin alias in MB, and no Latin in input
        credits = [
            {
                "name": "كايروكي",
                "artist": {
                    "id": "",
                    "name": "كايروكي",
                    "aliases": [{"name": "فرقة كايروكي", "locale": "ar"}],
                },
            }
        ]
        resolved = resolve_canonical_artist(credits, fallback_artist="كايروكي")
        self.assertEqual(resolved, "كايروكي")

    def test_rehome_album_folder_success_and_parent_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_path = Path(tmp_dir)
            arabic_artist_dir = base_path / "عمرو دياب"
            album_dir = arabic_artist_dir / "Saharna Ya Lail"
            album_dir.mkdir(parents=True)
            (album_dir / "01 - Track.mp3").write_text("dummy", encoding="utf-8")
            (album_dir / "01 - Track.lrc").write_text("[00:00.00] Lyrics", encoding="utf-8")

            # Re-home folder to canonical artist "Amr Diab"
            new_folder = rehome_album_folder(album_dir, "Amr Diab", base_dir=base_path)

            expected_path = base_path / "Amr Diab" / "Saharna Ya Lail"
            self.assertEqual(new_folder, expected_path)
            self.assertTrue((expected_path / "01 - Track.mp3").exists())
            self.assertTrue((expected_path / "01 - Track.lrc").exists())

            # Verify old album dir and empty parent artist folder were removed
            self.assertFalse(album_dir.exists())
            self.assertFalse(arabic_artist_dir.exists())

    def test_rehome_album_folder_merge_existing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_path = Path(tmp_dir)
            target_album_dir = base_path / "Amr Diab" / "Saharna Ya Lail"
            target_album_dir.mkdir(parents=True)
            (target_album_dir / "01 - Track.mp3").write_text("track 1", encoding="utf-8")

            source_artist_dir = base_path / "عمرو دياب"
            source_album_dir = source_artist_dir / "Saharna Ya Lail"
            source_album_dir.mkdir(parents=True)
            (source_album_dir / "02 - Track.mp3").write_text("track 2", encoding="utf-8")

            new_folder = rehome_album_folder(source_album_dir, "Amr Diab", base_dir=base_path)
            self.assertEqual(new_folder, target_album_dir)
            self.assertTrue((target_album_dir / "01 - Track.mp3").exists())
            self.assertTrue((target_album_dir / "02 - Track.mp3").exists())
            self.assertFalse(source_album_dir.exists())
            self.assertFalse(source_artist_dir.exists())

    def test_rehome_album_folder_security_traversal(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_path = Path(tmp_dir)
            outside_dir = base_path.parent / "unauthorized_folder"
            result = rehome_album_folder(outside_dir, "Amr Diab", base_dir=base_path)
            # Should return outside_dir unmodified without performing moves
            self.assertEqual(result, outside_dir.resolve())

    def test_multi_format_album_tagging(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            album_dir = Path(tmp_dir) / "عمرو دياب" / "Kol Hayaty"
            album_dir.mkdir(parents=True)

            # Create dummy MP3 with ID3 header
            mp3_file = album_dir / "01 - Song.mp3"
            mp3_file.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 100)

            # Create dummy Opus file
            opus_file = album_dir / "02 - Song.opus"
            opus_file.write_bytes(b"dummy opus")

            mock_mb_release = {
                "mbid": "test-mbid",
                "title": "Kol Hayaty",
                "artist": "Amr Diab",
                "date": "2018",
                "genre": "Pop",
                "tracks": [{"position": 1, "title": "Song 1", "length": 200.0}],
                "cover_bytes": b"fake_cover_bytes",
            }

            fake_opus_tags = {}
            mock_opus_instance = MagicMock()
            mock_opus_instance.__setitem__ = lambda self, k, v: fake_opus_tags.__setitem__(k, v)
            mock_opus_instance.__getitem__ = lambda self, k: fake_opus_tags.__getitem__(k)
            mock_opus_instance.save = MagicMock()

            with patch("services.tagger.search_musicbrainz_release", return_value=mock_mb_release), \
                 patch("services.tagger.OggOpus", return_value=mock_opus_instance), \
                 patch("services.tagger.search_genius_album", return_value=None):

                res = tag_album_hybrid(album_dir, "Kol Hayaty", "عمرو دياب")
                self.assertEqual(res["artist"], "Amr Diab")
                self.assertEqual(res["album"], "Kol Hayaty")

                # Verify loose cover.jpg was created
                self.assertTrue((album_dir / "cover.jpg").exists())

                # Verify MP3 tags updated with Latin canonical artist
                from mutagen.id3 import ID3
                id3 = ID3(str(mp3_file))
                self.assertEqual(str(id3["TPE1"].text[0]), "Amr Diab")
                self.assertEqual(str(id3["TPE2"].text[0]), "Amr Diab")
                self.assertEqual(str(id3["TALB"].text[0]), "Kol Hayaty")

                # Verify Opus instance had tags set
                self.assertEqual(fake_opus_tags.get("artist"), ["Amr Diab"])
                self.assertEqual(fake_opus_tags.get("albumartist"), ["Amr Diab"])
                self.assertEqual(fake_opus_tags.get("album"), ["Kol Hayaty"])
                mock_opus_instance.save.assert_called()


class TestQualitySettings(unittest.TestCase):
    def test_parse_quality_flag(self):
        from services.settings import parse_quality_flag

        url = "https://open.spotify.com/album/4cOdK2wGLETKBW3PvgPWqT"

        clean, flag = parse_quality_flag(f"{url} --flac")
        self.assertEqual(clean, url)
        self.assertEqual(flag, "flac")

        clean, flag = parse_quality_flag(f"{url} --OPUS")
        self.assertEqual(clean, url)
        self.assertEqual(flag, "opus")

        clean, flag = parse_quality_flag(f"{url} --mp3")
        self.assertEqual(clean, url)
        self.assertEqual(flag, "mp3")

        clean, flag = parse_quality_flag(f"{url} --auto")
        self.assertEqual(clean, url)
        self.assertEqual(flag, "auto")

        clean, flag = parse_quality_flag(f"{url} | Genius Album Match --flac")
        self.assertEqual(clean, f"{url} | Genius Album Match")
        self.assertEqual(flag, "flac")

        clean, flag = parse_quality_flag(url)
        self.assertEqual(clean, url)
        self.assertIsNone(flag)

    def test_quality_persistence(self):
        from services.settings import (
            get_quality_preference,
            set_quality_preference,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            custom_settings_file = Path(tmp_dir) / "settings.json"
            with patch("services.settings.SETTINGS_FILE", custom_settings_file), \
                 patch("services.settings.DATA_DIR", Path(tmp_dir)):

                self.assertEqual(get_quality_preference(), "auto")

                # Set user preference without altering global default
                set_quality_preference("opus", user_id=123, set_global=False)
                self.assertEqual(get_quality_preference(123), "opus")
                self.assertEqual(get_quality_preference(456), "auto")
                self.assertEqual(get_quality_preference(), "auto")

                # Set global default
                set_quality_preference("mp3", set_global=True)
                self.assertEqual(get_quality_preference(), "mp3")
                self.assertEqual(get_quality_preference(456), "mp3")
                # User 123 still has explicit override
                self.assertEqual(get_quality_preference(123), "opus")

                # Invalid quality should raise ValueError
                with self.assertRaises(ValueError):
                    set_quality_preference("wav")


class TestFLACTaggingAndLyrics(unittest.TestCase):
    def test_flac_album_tagging(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            album_dir = Path(tmp_dir) / "Amr Diab" / "Kol Hayaty"
            album_dir.mkdir(parents=True)

            flac_file = album_dir / "01 - Song.flac"
            flac_file.write_bytes(b"dummy flac content")

            fake_flac_tags = {}
            mock_flac_instance = MagicMock()
            mock_flac_instance.__setitem__ = lambda self, k, v: fake_flac_tags.__setitem__(k, v)
            mock_flac_instance.__getitem__ = lambda self, k: fake_flac_tags.__getitem__(k)
            mock_flac_instance.clear_pictures = MagicMock()
            mock_flac_instance.add_picture = MagicMock()
            mock_flac_instance.save = MagicMock()

            mock_mb = {
                "mbid": "test-mbid",
                "title": "Kol Hayaty",
                "artist": "Amr Diab",
                "date": "2018",
                "genre": "Pop",
                "tracks": [{"position": 1, "title": "Song 1", "length": 200.0}],
                "cover_bytes": b"fake_cover_bytes",
            }

            with patch("services.tagger.search_musicbrainz_release", return_value=mock_mb), \
                 patch("services.tagger.FLAC", return_value=mock_flac_instance), \
                 patch("services.tagger.search_genius_album", return_value=None):

                res = tag_album_hybrid(album_dir, "Kol Hayaty", "Amr Diab")
                self.assertEqual(res["artist"], "Amr Diab")
                self.assertEqual(res["album"], "Kol Hayaty")

                # Verify Vorbis comments set
                self.assertEqual(fake_flac_tags.get("title"), ["Song 1"])
                self.assertEqual(fake_flac_tags.get("artist"), ["Amr Diab"])
                self.assertEqual(fake_flac_tags.get("album"), ["Kol Hayaty"])
                self.assertEqual(fake_flac_tags.get("tracknumber"), ["1"])
                self.assertEqual(fake_flac_tags.get("tracktotal"), ["1"])
                self.assertEqual(fake_flac_tags.get("discnumber"), ["1"])
                self.assertEqual(fake_flac_tags.get("disctotal"), ["1"])

                # Verify Picture added and file saved
                mock_flac_instance.add_picture.assert_called()
                mock_flac_instance.save.assert_called()

                # Verify loose cover.jpg created
                self.assertTrue((album_dir / "cover.jpg").exists())

    def test_lyrics_sync_detects_flac(self):
        from services.lyrics import sync_all_lrc_in_folder

        with tempfile.TemporaryDirectory() as tmp_dir:
            folder = Path(tmp_dir) / "Music"
            folder.mkdir()
            flac_file = folder / "01 - Artist - Title.flac"
            flac_file.write_bytes(b"dummy flac")

            mock_flac_inst = MagicMock()
            mock_flac_inst.get = lambda k, default: ["Title"] if k == "title" else ["Artist"]

            with patch("services.lyrics.fetch_and_save_lrc", return_value=True) as mock_fetch, \
                 patch("mutagen.flac.FLAC", return_value=mock_flac_inst):
                sync_all_lrc_in_folder(folder)
                mock_fetch.assert_called_once()
                args, _ = mock_fetch.call_args
                self.assertEqual(args[0], "Title")
                self.assertEqual(args[1], "Artist")
                self.assertEqual(args[2], folder / "01 - Artist - Title.lrc")


class TestDownloaderRouting(unittest.TestCase):
    def test_youtube_routing_opus_default(self):
        from services.downloader import run_pipeline

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("config.BASE_DOWNLOAD_DIR", Path(tmp_dir)), \
                 patch("shutil.which", return_value="/usr/bin/yt-dlp"), \
                 patch("subprocess.run") as mock_subproc, \
                 patch("services.downloader.tag_album_hybrid", return_value={"artist": "Amr Diab", "album": "Album"}), \
                 patch("services.downloader.sync_all_lrc_in_folder", return_value=1):

                mock_probe = MagicMock(returncode=0)
                mock_probe.stdout = '{"title": "Track", "uploader": "Amr Diab", "entries": []}'
                mock_dl = MagicMock(returncode=0)
                mock_subproc.side_effect = [mock_probe, mock_dl]

                run_pipeline("https://www.youtube.com/watch?v=123", quality="auto")

                dl_cmd = mock_subproc.call_args_list[1][0][0]
                self.assertIn("--audio-format", dl_cmd)
                fmt_idx = dl_cmd.index("--audio-format")
                self.assertEqual(dl_cmd[fmt_idx + 1], "opus")

    def test_youtube_routing_mp3(self):
        from services.downloader import run_pipeline

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("config.BASE_DOWNLOAD_DIR", Path(tmp_dir)), \
                 patch("shutil.which", return_value="/usr/bin/yt-dlp"), \
                 patch("subprocess.run") as mock_subproc, \
                 patch("services.downloader.tag_album_hybrid", return_value={"artist": "Amr Diab", "album": "Album"}), \
                 patch("services.downloader.sync_all_lrc_in_folder", return_value=1):

                mock_probe = MagicMock(returncode=0)
                mock_probe.stdout = '{"title": "Track", "uploader": "Amr Diab", "entries": []}'
                mock_dl = MagicMock(returncode=0)
                mock_subproc.side_effect = [mock_probe, mock_dl]

                run_pipeline("https://www.youtube.com/watch?v=123", quality="mp3")

                dl_cmd = mock_subproc.call_args_list[1][0][0]
                self.assertIn("--audio-format", dl_cmd)
                fmt_idx = dl_cmd.index("--audio-format")
                self.assertEqual(dl_cmd[fmt_idx + 1], "mp3")

    def test_spotify_forced_flac_failure_raises(self):
        from services.downloader import run_pipeline

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("config.BASE_DOWNLOAD_DIR", Path(tmp_dir)):
                # SpotiFLAC fails or produces no flac files
                with self.assertRaises(RuntimeError) as ctx:
                    run_pipeline("https://open.spotify.com/album/4cOdK2wGLETKBW3PvgPWqT", quality="flac")
                self.assertIn("FLAC could not be resolved", str(ctx.exception))

    def test_spotify_auto_falls_back_to_opus(self):
        from services.downloader import run_pipeline

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("config.BASE_DOWNLOAD_DIR", Path(tmp_dir)), \
                 patch("services.downloader.find_spotdl_binary", return_value="/usr/bin/spotdl"), \
                 patch("subprocess.run") as mock_subproc, \
                 patch("services.downloader.tag_playlist_hybrid", return_value={"artist": "Various", "album": "Collection"}), \
                 patch("services.downloader.sync_all_lrc_in_folder", return_value=1):

                # Fake that spotdl created an opus file
                def fake_spotdl_run(cmd, **kwargs):
                    if "download" in cmd:
                        # Create dummy opus file in target folder
                        out_dir = Path(cmd[4]).parent
                        (out_dir / "Artist - Track.opus").write_bytes(b"dummy opus")
                    return MagicMock(returncode=0)

                mock_subproc.side_effect = fake_spotdl_run

                target_folder, meta = run_pipeline(
                    "https://open.spotify.com/album/4cOdK2wGLETKBW3PvgPWqT", quality="auto"
                )

                # Verify spotdl was called with --format opus
                call_args = mock_subproc.call_args[0][0]
                self.assertIn("--format", call_args)
                fmt_idx = call_args.index("--format")
                self.assertEqual(call_args[fmt_idx + 1], "opus")

    def test_spotify_explicit_mp3_skips_spotiflac(self):
        from services.downloader import run_pipeline

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("config.BASE_DOWNLOAD_DIR", Path(tmp_dir)), \
                 patch("services.downloader.find_spotdl_binary", return_value="/usr/bin/spotdl"), \
                 patch("subprocess.run") as mock_subproc, \
                 patch("services.downloader.tag_playlist_hybrid", return_value={"artist": "Various", "album": "Collection"}), \
                 patch("services.downloader.sync_all_lrc_in_folder", return_value=1):

                def fake_spotdl_run(cmd, **kwargs):
                    if "download" in cmd:
                        out_dir = Path(cmd[4]).parent
                        (out_dir / "Artist - Track.mp3").write_bytes(b"dummy mp3")
                    return MagicMock(returncode=0)

                mock_subproc.side_effect = fake_spotdl_run

                target_folder, meta = run_pipeline(
                    "https://open.spotify.com/album/4cOdK2wGLETKBW3PvgPWqT", quality="mp3"
                )

                call_args = mock_subproc.call_args[0][0]
                self.assertIn("--format", call_args)
                fmt_idx = call_args.index("--format")
                self.assertEqual(call_args[fmt_idx + 1], "mp3")


class TestUniversalAudioServices(unittest.TestCase):
    def test_sync_all_lrc_scans_all_supported_extensions(self):
        from services.lyrics import sync_all_lrc_in_folder

        with tempfile.TemporaryDirectory() as tmp_dir:
            folder = Path(tmp_dir)
            (folder / "Artist - Song1.flac").write_text("dummy", encoding="utf-8")
            (folder / "Artist - Song2.opus").write_text("dummy", encoding="utf-8")
            (folder / "Artist - Song3.m4a").write_text("dummy", encoding="utf-8")
            (folder / "Artist - Song4.mp3").write_text("dummy", encoding="utf-8")

            with patch("services.lyrics.fetch_and_save_lrc") as mock_fetch:
                def fake_save(title, artist, target_lrc):
                    Path(target_lrc).write_text("[00:00.00] test", encoding="utf-8")
                    return True

                mock_fetch.side_effect = fake_save
                total_lrc = sync_all_lrc_in_folder(folder)

                self.assertEqual(mock_fetch.call_count, 4)
                self.assertEqual(total_lrc, 4)
                self.assertTrue((folder / "Artist - Song1.lrc").exists())
                self.assertTrue((folder / "Artist - Song2.lrc").exists())
                self.assertTrue((folder / "Artist - Song3.lrc").exists())
                self.assertTrue((folder / "Artist - Song4.lrc").exists())

    def test_find_best_track_match_duration_tolerance(self):
        from services.tagger import find_best_track_match

        with tempfile.TemporaryDirectory() as tmp_dir:
            track_file = Path(tmp_dir) / "01 - My Track.flac"
            track_file.write_text("dummy", encoding="utf-8")

            mb_tracks = [
                {"title": "My Track", "position": 1, "length": 210.0},
                {"title": "Other Track", "position": 2, "length": 180.0},
            ]
            assigned = set()

            mock_audio = MagicMock()
            mock_audio.info.length = 211.5  # diff 1.5s <= 2.5s gives +80 duration bonus

            with patch("mutagen.File", return_value=mock_audio):
                title, pos = find_best_track_match(track_file, "My Track", 1, mb_tracks, assigned)
                self.assertEqual(title, "My Track")
                self.assertEqual(pos, 1)

    def test_library_albums_multi_format_counting(self):
        from services.library_browser import get_library_albums

        with tempfile.TemporaryDirectory() as tmp_dir:
            base_path = Path(tmp_dir)
            album_dir = base_path / "Cool Artist" / "Mixed Album"
            album_dir.mkdir(parents=True)

            (album_dir / "01 - S1.flac").write_text("dummy", encoding="utf-8")
            (album_dir / "02 - S2.opus").write_text("dummy", encoding="utf-8")
            (album_dir / "03 - S3.m4a").write_text("dummy", encoding="utf-8")
            (album_dir / "04 - S4.mp3").write_text("dummy", encoding="utf-8")
            (album_dir / "01 - S1.lrc").write_text("[00:00.00] L1", encoding="utf-8")

            with patch("config.BASE_DOWNLOAD_DIR", base_path):
                albums = get_library_albums()
                self.assertEqual(len(albums), 1)
                self.assertEqual(albums[0]["track_count"], 4)
                self.assertEqual(albums[0]["lrc_count"], 1)
                self.assertEqual(albums[0]["lyrics_status"], "synced")


if __name__ == "__main__":
    unittest.main()



