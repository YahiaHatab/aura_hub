"""Unit tests for FastAPI WebApp endpoints, HMAC security, and Navidrome dashboard API."""

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


if __name__ == "__main__":
    unittest.main()
