"""Unit tests for FastAPI WebApp endpoints, HMAC security, and Navidrome dashboard API."""

import hashlib
import hmac
import json
import time
import unittest
import urllib.parse
from unittest.mock import patch

from fastapi.testclient import TestClient

import config
from services.web import app, verify_telegram_init_data


def generate_test_init_data(user_id: int, bot_token: str = config.TELEGRAM_BOT_TOKEN) -> str:
    """Generates an authentic Telegram WebApp initData string signed with bot_token."""
    user_json = json.dumps({"id": user_id, "first_name": "Admin", "username": "admin_user"})
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
        self.non_admin_id = 999999999
        self.valid_admin_token = generate_test_init_data(self.admin_id)
        self.valid_non_admin_token = generate_test_init_data(self.non_admin_id)

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
        ok, user_data, err = verify_telegram_init_data(self.valid_admin_token, config.TELEGRAM_BOT_TOKEN)
        self.assertTrue(ok)
        self.assertIsNotNone(user_data)
        self.assertEqual(user_data["id"], self.admin_id)

        # 2. Tampered signature
        tampered_token = self.valid_admin_token.replace("hash=", "hash=badhash123")
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
        headers = {"Authorization": f"Bearer {self.valid_non_admin_token}"}
        res = self.client.get("/api/users", headers=headers)
        self.assertEqual(res.status_code, 403)

        # 3. Admin user signature -> 200 OK (with mock)
        with patch("services.navidrome.navidrome_client.get_users", return_value={"ok": True, "users": []}):
            admin_headers = {"Authorization": f"Bearer {self.valid_admin_token}"}
            res = self.client.get("/api/users", headers=admin_headers)
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["ok"])

    def test_user_management_endpoints(self):
        """Tests /api/users/create, /api/users/delete, and /api/users/reset-password."""
        headers = {"Authorization": f"Bearer {self.valid_admin_token}"}

        # Create User
        with patch("services.navidrome.navidrome_client.create_user", return_value={"ok": True}):
            res = self.client.post(
                "/api/users/create",
                json={"username": "tester", "password": "pass1234", "admin_role": False},
                headers=headers,
            )
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["ok"])

        # Reset Password
        with patch("services.navidrome.navidrome_client.update_user", return_value={"ok": True}):
            res = self.client.post(
                "/api/users/reset-password",
                json={"username": "tester"},
                headers=headers,
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
                headers=headers,
            )
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["ok"])

    def test_now_playing_and_server_endpoints(self):
        """Tests /api/nowplaying, /api/rescan, and /api/system."""
        headers = {"Authorization": f"Bearer {self.valid_admin_token}"}

        # Now Playing (/api and /hub/api)
        sample_streams = [{"username": "yahia", "title": "Hotel California", "artist": "Eagles", "album": "Hotel California", "player": "Symfonium", "bitRate": 320}]
        with patch("services.navidrome.navidrome_client.get_now_playing", return_value={"ok": True, "streams": sample_streams, "entries": sample_streams, "count": 1}):
            for ep in ["/api/nowplaying", "/hub/api/nowplaying"]:
                res = self.client.get(ep, headers=headers)
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertTrue(data["ok"])
                self.assertEqual(data["count"], 1)
                self.assertIn("streams", data)
                self.assertEqual(len(data["streams"]), 1)
                self.assertEqual(data["streams"][0]["player"], "Symfonium")

        # Rescan
        with patch("services.navidrome.navidrome_client.start_scan", return_value={"ok": True, "count": 100}):
            res = self.client.post("/api/rescan", headers=headers)
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["ok"])

        # System stats
        res = self.client.get("/api/system", headers=headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertIn("disk", data)


if __name__ == "__main__":
    unittest.main()
