"""Unit tests for FastAPI WebApp endpoints, HMAC security, and Navidrome dashboard API."""

import base64
import hashlib
import hmac
import json
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import config
from services.metadata import MetadataCandidate, UnifiedAlbumMetadata, UnifiedTrackMetadata
from services.web import app, verify_telegram_init_data


def generate_test_init_data(user_id: int, bot_token: str = config.TELEGRAM_BOT_TOKEN) -> str:
    """Generates an authentic Telegram WebApp initData string signed with bot_token."""
    user_json = json.dumps({"id": user_id, "first_name": "TestUser", "username": f"user_{user_id}"})
    auth_date = str(int(time.time()))
    pairs = [
        f"auth_date={auth_date}",
        "query_id=AAG8a9kBAAAAALxr2QEw-4_q",
        f"user={user_json}",
    ]
    pairs.sort()
    data_check_string = "\n".join(pairs)
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    # URL-encode parameters as Telegram WebApp client does
    return (
        f"auth_date={auth_date}"
        f"&query_id=AAG8a9kBAAAAALxr2QEw-4_q"
        f"&user={urllib.parse.quote(user_json)}"
        f"&hash={sig}"
    )


class TestWebAppAndSecurity(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.admin_id = 1497076788
        self.regular_user_id = 888888888
        self.unauthorized_user_id = 999999999

        # Ensure regular_user_id is in ALLOWED_USER_IDS for tests
        config.ALLOWED_USER_IDS.add(self.regular_user_id)

        self.admin_headers = {"Authorization": f"Bearer {generate_test_init_data(self.admin_id)}"}
        self.regular_headers = {"Authorization": f"Bearer {generate_test_init_data(self.regular_user_id)}"}
        self.unauth_headers = {"Authorization": f"Bearer {generate_test_init_data(self.unauthorized_user_id)}"}

    def test_serve_dashboard_html(self):
        """Tests that GET /, /webapp, /hub, /hub/, and /hub/webapp serve index.html."""
        for path in ["/", "/webapp", "/hub", "/hub/", "/hub/webapp"]:
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, f"Failed for path {path}")
            self.assertIn("Aura Hub", res.text)
            self.assertIn("telegram-web-app.js", res.text)

    def test_cors_headers_present(self):
        """Tests that CORS headers are returned for cross-origin or proxied requests."""
        res = self.client.get("/", headers={"Origin": "https://h-navidrome.duckdns.org"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("access-control-allow-origin"), "*")

    def test_hmac_verification_algorithm(self):
        """Tests cryptographic verification of Telegram initData."""
        # 1. Valid signature
        token = generate_test_init_data(self.admin_id)
        ok, user_data, err = verify_telegram_init_data(token, config.TELEGRAM_BOT_TOKEN)
        self.assertTrue(ok)
        self.assertIsNotNone(user_data)
        self.assertEqual(user_data["id"], self.admin_id)

        # 2. Tampered signature
        tampered_token = token.replace("hash=", "hash=badhash123")
        ok, user_data, err = verify_telegram_init_data(tampered_token, config.TELEGRAM_BOT_TOKEN)
        self.assertFalse(ok)
        self.assertIn("Invalid Telegram HMAC signature", err)

        # 3. Missing initData
        ok, user_data, err = verify_telegram_init_data("", config.TELEGRAM_BOT_TOKEN)
        self.assertFalse(ok)

    def test_auth_protection_on_api(self):
        """Verifies 401 when unauthenticated and 403 when not an authorized admin."""
        # 1. No Authorization header -> 401
        res = self.client.get("/api/users")
        self.assertEqual(res.status_code, 401)

        # 2. Non-admin user signature -> 403 Forbidden
        res = self.client.get("/api/users", headers=self.regular_headers)
        self.assertEqual(res.status_code, 403)

        # 3. Unauthorized user signature -> 403 Forbidden
        res = self.client.get("/api/users", headers=self.unauth_headers)
        self.assertEqual(res.status_code, 403)

        # 4. Admin user signature -> 200 OK (with mock)
        with patch("services.navidrome.navidrome_client.get_users", return_value={"ok": True, "users": []}):
            res = self.client.get("/api/users", headers=self.admin_headers)
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["ok"])

    def test_me_endpoint(self):
        """Tests /api/me returns user profile and correct admin flag."""
        # Admin
        res_admin = self.client.get("/api/me", headers=self.admin_headers)
        self.assertEqual(res_admin.status_code, 200)
        self.assertTrue(res_admin.json()["is_admin"])
        self.assertEqual(res_admin.json()["user_id"], self.admin_id)

        # Regular authorized user
        res_user = self.client.get("/api/me", headers=self.regular_headers)
        self.assertEqual(res_user.status_code, 200)
        self.assertFalse(res_user.json()["is_admin"])
        self.assertEqual(res_user.json()["user_id"], self.regular_user_id)

        # Unauthorized user
        res_unauth = self.client.get("/api/me", headers=self.unauth_headers)
        self.assertEqual(res_unauth.status_code, 403)

    def test_requests_api(self):
        """Tests /api/requests/submit, /api/requests, and /api/requests/action."""
        # Submit request as regular user
        submit_res = self.client.post(
            "/api/requests/submit",
            json={"query_or_url": "Amr Diab - Nour El Ein"},
            headers=self.regular_headers,
        )
        self.assertEqual(submit_res.status_code, 200)
        req_data = submit_res.json()["request"]
        req_id = req_data["id"]
        self.assertEqual(req_data["status"], "PENDING")
        self.assertEqual(req_data["query_or_url"], "Amr Diab - Nour El Ein")

        # Regular user views own requests
        get_res = self.client.get("/api/requests", headers=self.regular_headers)
        self.assertEqual(get_res.status_code, 200)
        user_reqs = get_res.json()["requests"]
        self.assertTrue(any(r["id"] == req_id for r in user_reqs))

        # Regular user cannot approve or reject -> 403
        action_forbidden = self.client.post(
            "/api/requests/action",
            json={"request_id": req_id, "action": "approve"},
            headers=self.regular_headers,
        )
        self.assertEqual(action_forbidden.status_code, 403)

        # Admin rejects request -> 200
        action_admin = self.client.post(
            "/api/requests/action",
            json={"request_id": req_id, "action": "reject"},
            headers=self.admin_headers,
        )
        self.assertEqual(action_admin.status_code, 200)
        self.assertTrue(action_admin.json()["ok"])

        # Non-admin cannot clear requests -> 403
        clear_non_admin = self.client.post(
            "/api/requests/clear",
            json={"status": "completed_only"},
            headers=self.regular_headers,
        )
        self.assertEqual(clear_non_admin.status_code, 403)

        # Admin clears past requests -> 200
        clear_admin = self.client.post(
            "/api/requests/clear",
            json={"status": "completed_only"},
            headers=self.admin_headers,
        )
        self.assertEqual(clear_admin.status_code, 200)
        self.assertTrue(clear_admin.json()["ok"])
        self.assertIn("cleared", clear_admin.json())

    def test_downloader_and_tasks_api(self):
        """Tests /api/download and /api/tasks endpoints."""
        # Non-admin cannot download -> 403
        res_non_admin = self.client.post(
            "/api/download",
            json={"url": "https://www.youtube.com/watch?v=test"},
            headers=self.regular_headers,
        )
        self.assertEqual(res_non_admin.status_code, 403)

        # Admin triggers download
        with patch("services.tasks.executor.submit") as mock_submit:
            res_admin = self.client.post(
                "/api/download",
                json={"url": "https://www.youtube.com/watch?v=test"},
                headers=self.admin_headers,
            )
            self.assertEqual(res_admin.status_code, 200)
            data = res_admin.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["task"]["url"], "https://www.youtube.com/watch?v=test")
            self.assertEqual(data["task"]["source_url"], "https://www.youtube.com/watch?v=test")
            self.assertIn("title", data["task"])
            self.assertIn("artist", data["task"])
            self.assertIn("cover_url", data["task"])
            mock_submit.assert_called_once()

        # Admin polls tasks
        tasks_res = self.client.get("/api/tasks", headers=self.admin_headers)
        self.assertEqual(tasks_res.status_code, 200)
        self.assertTrue(tasks_res.json()["ok"])
        self.assertTrue(isinstance(tasks_res.json()["tasks"], list))

    def test_library_and_cover_api(self):
        """Tests /api/library, /api/cover, /api/library/refetch-lyrics, and /api/library/delete."""
        # Library listing
        mock_albums = [{
            "folder": "Artist/Album",
            "artist": "Artist",
            "album": "Album",
            "track_count": 10,
            "lrc_count": 10,
            "has_cover": True,
            "lyrics_status": "synced",
        }]
        with patch("services.web.get_library_albums", return_value=mock_albums):
            res = self.client.get("/api/library", headers=self.regular_headers)
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["count"], 1)

        # Path traversal protection on /api/cover -> 400
        res_traversal = self.client.get("/api/cover?path=../../etc/passwd")
        self.assertEqual(res_traversal.status_code, 400)

        # Safe cover serving
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            album_dir = tmp_path / "MyArtist" / "MyAlbum"
            album_dir.mkdir(parents=True)
            cover_file = album_dir / "cover.jpg"
            cover_file.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")

            with patch("config.BASE_DOWNLOAD_DIR", tmp_path):
                cover_res = self.client.get("/api/cover?path=MyArtist/MyAlbum")
                self.assertEqual(cover_res.status_code, 200)
                self.assertEqual(cover_res.headers.get("content-type"), "image/jpeg")

        # Cover fallback placeholder when folder has no cover
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            album_dir = tmp_path / "EmptyArtist" / "EmptyAlbum"
            album_dir.mkdir(parents=True)
            with patch("config.BASE_DOWNLOAD_DIR", tmp_path):
                cover_res = self.client.get("/api/cover?path=EmptyArtist/EmptyAlbum")
                self.assertEqual(cover_res.status_code, 200)
                self.assertEqual(cover_res.headers.get("content-type"), "image/svg+xml")
                self.assertIn("<svg", cover_res.text)

        # Refetch lyrics (admin only)
        with patch("services.web.refetch_album_lyrics", return_value={"ok": True, "message": "Synced"}):
            refetch_res = self.client.post(
                "/api/library/refetch-lyrics",
                json={"folder": "MyArtist/MyAlbum"},
                headers=self.admin_headers,
            )
            self.assertEqual(refetch_res.status_code, 200)
            self.assertTrue(refetch_res.json()["ok"])

        # Delete album (admin only)
        with patch("services.web.remove_album", return_value=(True, "Deleted")):
            del_res = self.client.post(
                "/api/library/delete",
                json={"folder": "MyArtist/MyAlbum"},
                headers=self.admin_headers,
            )
            self.assertEqual(del_res.status_code, 200)
            self.assertTrue(del_res.json()["ok"])

    def test_user_management_endpoints(self):
        """Tests /api/users/create, /api/users/delete, and /api/users/reset-password."""
        # Create User
        with patch("services.navidrome.navidrome_client.create_user", return_value={"ok": True}):
            res = self.client.post(
                "/api/users/create",
                json={"username": "tester", "password": "pass1234", "admin_role": False},
                headers=self.admin_headers,
            )
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["ok"])

        # Reset Password
        with patch("services.navidrome.navidrome_client.update_user", return_value={"ok": True}):
            res = self.client.post(
                "/api/users/reset-password",
                json={"username": "tester"},
                headers=self.admin_headers,
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data["ok"])
            self.assertTrue(len(data["password"]) >= 10)

        # Delete User
        with patch("services.navidrome.navidrome_client.delete_user", return_value={"ok": True}):
            res = self.client.post(
                "/api/users/delete",
                json={"username": "tester"},
                headers=self.admin_headers,
            )
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["ok"])

    def test_now_playing_and_server_endpoints(self):
        """Tests /api/nowplaying, /api/rescan, and /api/system."""
        # Now Playing (/api and /hub/api)
        sample_streams = [{
            "username": "yahia",
            "title": "Hotel California",
            "artist": "Eagles",
            "album": "Hotel California",
            "player": "Symfonium",
            "bitRate": 320,
        }]
        with patch("services.navidrome.navidrome_client.get_now_playing", return_value={"ok": True, "streams": sample_streams, "entries": sample_streams, "count": 1}):
            for ep in ["/api/nowplaying", "/hub/api/nowplaying"]:
                res = self.client.get(ep, headers=self.regular_headers)
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertTrue(data["ok"])
                self.assertEqual(data["count"], 1)
                self.assertIn("streams", data)
                self.assertEqual(len(data["streams"]), 1)
                self.assertEqual(data["streams"][0]["player"], "Symfonium")

        # Rescan
        with patch("services.navidrome.navidrome_client.start_scan", return_value={"ok": True, "count": 100}):
            res = self.client.post("/api/rescan", headers=self.admin_headers)
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["ok"])

        # System stats
        res = self.client.get("/api/system", headers=self.admin_headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertIn("disk", data)
        self.assertIn("audio_count", data)
        self.assertIn("mp3_count", data)

    def test_metadata_search_endpoint(self):
        """Tests GET /api/metadata/search for albums and tracks."""
        # 1. Missing query -> 400
        res = self.client.get("/api/metadata/search?query=", headers=self.regular_headers)
        self.assertEqual(res.status_code, 400)

        # 2. Album search candidate list
        mock_cand = MetadataCandidate(
            source="MusicBrainz",
            confidence_score=95.0,
            is_recommended=True,
            album_data=UnifiedAlbumMetadata(
                album="Fancy that",
                artist="PinkPantheress",
                year="2025",
                genre="Pop",
                genres=["Pop", "Drum and Bass"],
                tracks=[UnifiedTrackMetadata(title="Tonight", track_number=1, duration_seconds=180.0)],
            ),
            preview={
                "title": "Fancy that",
                "artist": "PinkPantheress",
                "year": "2025",
                "genres": "Pop",
                "track_count": 1,
                "has_cover": True,
                "has_lyrics": True,
            },
        )
        with patch("services.web.search_album_metadata_candidates_async", return_value=[mock_cand]):
            res = self.client.get(
                "/api/metadata/search?query=Fancy+that&artist=PinkPantheress&type=album",
                headers=self.regular_headers,
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["count"], 1)
            self.assertEqual(data["candidates"][0]["source"], "MusicBrainz")
            self.assertTrue(data["candidates"][0]["is_recommended"])

        # 3. Track search candidate list
        mock_trk_cand = MetadataCandidate(
            source="Deezer",
            confidence_score=88.0,
            is_recommended=True,
            track_data=UnifiedTrackMetadata(
                title="Boy's a liar",
                artist="PinkPantheress",
                album="Boy's a liar",
                year="2022",
                genre="Pop",
            ),
            preview={"title": "Boy's a liar", "artist": "PinkPantheress", "has_cover": True},
        )
        with patch("services.web.search_track_metadata_candidates_async", return_value=[mock_trk_cand]):
            res = self.client.get(
                "/api/metadata/search?query=Boy's+a+liar&artist=PinkPantheress&type=track",
                headers=self.regular_headers,
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["candidates"][0]["source"], "Deezer")

    def test_metadata_inspect_endpoint(self):
        """Tests GET /api/metadata/inspect for both albums and individual tracks."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            album_dir = tmp_path / "Elissa" / "Saharna Ya Leil"
            album_dir.mkdir(parents=True)
            track_file = album_dir / "01 - Saharna Ya Leil.mp3"
            track_file.write_bytes(b"ID3 dummy content for test")

            with patch("config.BASE_DOWNLOAD_DIR", tmp_path):
                # Inspect album directory
                res_album = self.client.get(
                    "/api/metadata/inspect?path=Elissa/Saharna Ya Leil",
                    headers=self.regular_headers,
                )
                self.assertEqual(res_album.status_code, 200)
                data_alb = res_album.json()
                self.assertTrue(data_alb["ok"])
                self.assertEqual(data_alb["type"], "album")

                # Inspect track file
                res_track = self.client.get(
                    "/api/metadata/inspect?path=Elissa/Saharna Ya Leil/01 - Saharna Ya Leil.mp3",
                    headers=self.regular_headers,
                )
                self.assertEqual(res_track.status_code, 200)
                data_trk = res_track.json()
                self.assertTrue(data_trk["ok"])
                self.assertEqual(data_trk["type"], "track")

                # Path traversal attack -> 400
                res_bad = self.client.get(
                    "/api/metadata/inspect?path=../../etc/passwd",
                    headers=self.regular_headers,
                )
                self.assertEqual(res_bad.status_code, 400)

    def test_metadata_apply_endpoint(self):
        """Tests POST /api/metadata/apply with authorization checks and tagging execution."""
        payload = {
            "path": "Elissa/Saharna Ya Leil",
            "type": "album",
            "candidate": {
                "source": "MusicBrainz",
                "album_data": {
                    "album": "Saharna Ya Leil",
                    "artist": "Elissa",
                    "year": "2016",
                    "genre": "Arabic Pop",
                    "tracks": [{"title": "Saharna Ya Leil", "track_number": 1}],
                },
            },
            "rescan": False,
        }

        # Non-admin user -> 403 Forbidden
        res_non_admin = self.client.post(
            "/api/metadata/apply",
            json=payload,
            headers=self.regular_headers,
        )
        self.assertEqual(res_non_admin.status_code, 403)

        # Admin user applying to album
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            album_dir = tmp_path / "Elissa" / "Saharna Ya Leil"
            album_dir.mkdir(parents=True)
            with patch("config.BASE_DOWNLOAD_DIR", tmp_path):
                with patch(
                    "services.web.apply_unified_metadata_to_album",
                    return_value={"album": "Saharna Ya Leil", "artist": "Elissa"},
                ):
                    res = self.client.post(
                        "/api/metadata/apply",
                        json=payload,
                        headers=self.admin_headers,
                    )
                    self.assertEqual(res.status_code, 200)
                    data = res.json()
                    self.assertTrue(data["ok"])
                    self.assertIn("Successfully applied", data["message"])

    def test_tags_inspect_and_cover_endpoints(self):
        """Tests GET /api/tags/inspect and GET /api/tags/cover."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            song_file = tmp_path / "Artist" / "Album" / "track.mp3"
            song_file.parent.mkdir(parents=True)
            song_file.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 100)

            mock_tags = {
                "title": "Track Name",
                "artist": "Artist Name",
                "album": "Album Name",
                "track_number": 1,
                "has_cover": True,
            }

            with patch("config.BASE_DOWNLOAD_DIR", tmp_path):
                with patch("services.web.read_tags", return_value=mock_tags):
                    res = self.client.get("/api/tags/inspect?path=Artist/Album/track.mp3", headers=self.regular_headers)
                    self.assertEqual(res.status_code, 200)
                    data = res.json()
                    self.assertTrue(data["ok"])
                    self.assertEqual(data["tags"]["title"], "Track Name")

                # Test /api/tags/cover
                with patch("services.web.extract_cover_bytes", return_value=(b"\xff\xd8\xff\xe0JFIF", "image/jpeg")):
                    res_cov = self.client.get("/api/tags/cover?path=Artist/Album/track.mp3", headers=self.regular_headers)
                    self.assertEqual(res_cov.status_code, 200)
                    self.assertEqual(res_cov.headers["content-type"], "image/jpeg")
                    self.assertEqual(res_cov.content, b"\xff\xd8\xff\xe0JFIF")

    def test_metadata_match_and_lyrics_fetch_endpoints(self):
        """Tests GET /api/metadata/match and GET /api/lyrics/fetch."""
        mock_candidates = [
            {
                "source": "MusicBrainz",
                "confidence_score": 95.0,
                "is_recommended": True,
                "fields": {"title": "Matched Track", "artist": "Matched Artist"},
            }
        ]

        with patch("services.web.search_metadata_async", return_value=mock_candidates):
            res = self.client.get("/api/metadata/match?query=Matched+Track&type=track", headers=self.regular_headers)
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data["ok"])
            self.assertEqual(len(data["candidates"]), 1)
            self.assertEqual(data["candidates"][0]["source"], "MusicBrainz")

        with patch("services.web.fetch_lrclib_lyrics_async", return_value={"synced": "[00:10.00] Synced line", "plain": "Plain line"}):
            res_lrc = self.client.get("/api/lyrics/fetch?track=Matched+Track&artist=Matched+Artist", headers=self.regular_headers)
            self.assertEqual(res_lrc.status_code, 200)
            data_lrc = res_lrc.json()
            self.assertTrue(data_lrc["ok"])
            self.assertEqual(data_lrc["lyrics_synced"], "[00:10.00] Synced line")

    def test_tags_commit_endpoint(self):
        """Tests POST /api/tags/commit with authentication checks, tag writing, and rescan."""
        payload = {
            "path": "Artist/Album/track.mp3",
            "fields": {
                "title": "New Title",
                "genre": "New Genre",
                "producers": ["Producer X"],
            },
            "lyrics_lrc": "[00:01.00] New LRC line",
            "cover_data_base64": "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0JFIF").decode("ascii"),
            "rescan": True,
        }

        # Non-admin -> 403 Forbidden
        res_user = self.client.post("/api/tags/commit", json=payload, headers=self.regular_headers)
        self.assertEqual(res_user.status_code, 403)

        # Admin user
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            target_f = tmp_path / "Artist" / "Album" / "track.mp3"
            target_f.parent.mkdir(parents=True)
            target_f.write_bytes(b"dummy")

            with patch("config.BASE_DOWNLOAD_DIR", tmp_path):
                with patch("services.web.write_tags", return_value=True) as mock_write:
                    with patch("services.web.read_tags", return_value={"title": "New Title", "genre": "New Genre"}):
                        with patch("services.navidrome.scan_path", return_value={"ok": True}):
                            res_admin = self.client.post("/api/tags/commit", json=payload, headers=self.admin_headers)
                            self.assertEqual(res_admin.status_code, 200)
                            data = res_admin.json()
                            self.assertTrue(data["ok"])
                            self.assertIn("Successfully updated", data["message"])
                            mock_write.assert_called_once()


if __name__ == "__main__":
    unittest.main()


