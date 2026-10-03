"""Subsonic and Navidrome REST client for Aura Hub.

Communicates with Navidrome's Subsonic API endpoint to trigger library scans,
check scan status, and verify server connectivity.
"""

import hashlib
import json
import logging
import secrets
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

import config

logger = logging.getLogger(__name__)


class NavidromeClient:
    """REST Client for Subsonic/Navidrome API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.base_url = (base_url or config.NAVIDROME_URL).rstrip("/")
        self.username = username if username is not None else config.NAVIDROME_USER
        self.password = password if password is not None else config.NAVIDROME_PASS
        self.client_name = "AuraHub"
        self.api_version = "1.16.1"

    def is_configured(self) -> bool:
        """Returns True if Navidrome URL, username, and password are configured."""
        return bool(self.base_url and self.username and self.password)

    def _build_auth_params(self) -> Dict[str, str]:
        """Generates standard Subsonic token authentication parameters (t = md5(password + salt))."""
        salt = secrets.token_hex(6)
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()
        return {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": self.api_version,
            "c": self.client_name,
            "f": "json",
        }

    def _request(self, endpoint: str, extra_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Sends an authenticated GET request to Navidrome's Subsonic REST API."""
        if not self.is_configured():
            return {
                "ok": False,
                "message": (
                    "Navidrome credentials are not configured. "
                    "Please set NAVIDROME_USER and NAVIDROME_PASS in config.py or environment."
                ),
            }

        params = self._build_auth_params()
        if extra_params:
            for k, v in extra_params.items():
                if isinstance(v, bool):
                    params[k] = "true" if v else "false"
                else:
                    params[k] = str(v)

        query_string = urllib.parse.urlencode(params)
        url = f"{self.base_url}/rest/{endpoint}.view?{query_string}"

        req = urllib.request.Request(url, headers={"User-Agent": "AuraHub/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            sub_resp = data.get("subsonic-response", {})
            status = sub_resp.get("status")

            if status == "ok":
                return {"ok": True, "data": sub_resp}

            error = sub_resp.get("error", {})
            code = error.get("code", "unknown")
            msg = error.get("message", "Unknown error returned from Navidrome")
            return {"ok": False, "message": f"Navidrome Error ({code}): {msg}"}

        except urllib.error.URLError as e:
            logger.warning(f"Navidrome connection error: {e}")
            return {
                "ok": False,
                "message": f"Could not reach Navidrome at {self.base_url}: {e.reason}",
            }
        except Exception as e:
            logger.warning(f"Navidrome request failed: {e}")
            return {"ok": False, "message": f"Unexpected error contacting Navidrome: {e}"}

    def ping(self) -> Dict[str, Any]:
        """Tests connectivity and authentication with the Navidrome server."""
        res = self._request("ping")
        if res.get("ok"):
            sub_resp = res.get("data", {})
            server_version = sub_resp.get("serverVersion", sub_resp.get("version", "Unknown"))
            server_type = sub_resp.get("type", "navidrome")
            return {
                "ok": True,
                "message": f"Connected to {server_type.title()} (v{server_version}) successfully!",
                "version": server_version,
            }
        return res

    def start_scan(self, full_scan: bool = False) -> Dict[str, Any]:
        """Triggers an immediate Subsonic library scan (/rest/startScan)."""
        extra = {"fullScan": full_scan} if full_scan else {}
        res = self._request("startScan", extra)
        if res.get("ok"):
            sub_resp = res.get("data", {})
            scan_status = sub_resp.get("scanStatus", {})
            count = scan_status.get("count", 0)
            scanning = scan_status.get("scanning", True)
            return {
                "ok": True,
                "message": "Navidrome library scan initiated successfully.",
                "scanning": scanning,
                "count": count,
            }
        return res

    def get_scan_status(self) -> Dict[str, Any]:
        """Checks current library scanning progress (/rest/getScanStatus)."""
        res = self._request("getScanStatus")
        if res.get("ok"):
            sub_resp = res.get("data", {})
            scan_status = sub_resp.get("scanStatus", {})
            scanning = scan_status.get("scanning", False)
            count = scan_status.get("count", 0)
            return {
                "ok": True,
                "scanning": scanning,
                "count": count,
                "message": (
                    f"Scan in progress: {count} tracks scanned so far..."
                    if scanning
                    else f"Scan complete. Total tracks indexed: {count}."
                ),
            }
        return res

    def get_users(self) -> Dict[str, Any]:
        """Fetches list of all users from Navidrome (/rest/getUsers)."""
        res = self._request("getUsers")
        if res.get("ok"):
            sub_resp = res.get("data", {})
            users_container = sub_resp.get("users", {})
            raw_users = users_container.get("user", [])
            if isinstance(raw_users, dict):
                raw_users = [raw_users]
            return {"ok": True, "users": raw_users}
        return res

    def get_user(self, username: str) -> Dict[str, Any]:
        """Fetches details for a specific user (/rest/getUser)."""
        res = self._request("getUser", {"username": username})
        if res.get("ok"):
            sub_resp = res.get("data", {})
            user_data = sub_resp.get("user", {})
            return {"ok": True, "user": user_data}
        return res

    def create_user(
        self,
        username: str,
        password: str,
        email: str = "",
        admin_role: bool = False,
        stream_role: bool = True,
        download_role: bool = True,
    ) -> Dict[str, Any]:
        """Creates a new user account in Navidrome (/rest/createUser)."""
        extra: Dict[str, Any] = {
            "username": username,
            "password": password,
            "adminRole": admin_role,
            "streamRole": stream_role,
            "downloadRole": download_role,
        }
        if email:
            extra["email"] = email

        res = self._request("createUser", extra)
        if res.get("ok"):
            return {
                "ok": True,
                "message": f"User '{username}' created successfully.",
            }
        return res

    def update_user(
        self,
        username: str,
        password: Optional[str] = None,
        email: Optional[str] = None,
        admin_role: Optional[bool] = None,
        stream_role: Optional[bool] = None,
        download_role: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Updates an existing user account in Navidrome (/rest/updateUser)."""
        extra: Dict[str, Any] = {"username": username}
        if password is not None:
            extra["password"] = password
        if email is not None:
            extra["email"] = email
        if admin_role is not None:
            extra["adminRole"] = admin_role
        if stream_role is not None:
            extra["streamRole"] = stream_role
        if download_role is not None:
            extra["downloadRole"] = download_role

        res = self._request("updateUser", extra)
        if res.get("ok"):
            return {
                "ok": True,
                "message": f"User '{username}' updated successfully.",
            }
        return res

    def delete_user(self, username: str) -> Dict[str, Any]:
        """Deletes a user account from Navidrome (/rest/deleteUser)."""
        res = self._request("deleteUser", {"username": username})
        if res.get("ok"):
            return {
                "ok": True,
                "message": f"User '{username}' deleted successfully.",
            }
        return res


# Global singleton instance initialized from config
navidrome_client = NavidromeClient()
