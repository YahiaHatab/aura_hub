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
from typing import Any, Dict, Optional, Tuple

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
        self._jwt_token: Optional[str] = None

    def is_configured(self) -> bool:
        """Returns True if Navidrome URL, username, and password are configured."""
        return bool(self.base_url and self.username and self.password)

    def _get_native_token(self, force_refresh: bool = False) -> Optional[str]:
        """Authenticates with Navidrome's native REST API (/auth/login) to obtain a JWT bearer token."""
        if not self.is_configured():
            return None

        if self._jwt_token and not force_refresh:
            return self._jwt_token

        try:
            login_url = f"{self.base_url}/auth/login"
            payload = json.dumps({"username": self.username, "password": self.password}).encode("utf-8")
            req = urllib.request.Request(
                login_url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "AuraHub/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                token = data.get("token")
                if token:
                    self._jwt_token = token
                    return token
        except Exception as e:
            logger.debug(f"Native Navidrome login failed: {e}")

        return None

    def _native_request(
        self,
        method: str,
        endpoint: str,
        body: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Any]:
        """Sends an authenticated request to Navidrome's native REST API (/api/...)."""
        token = self._get_native_token()
        if not token:
            return False, "Failed to authenticate with Navidrome native API."

        url = f"{self.base_url}/api/{endpoint}"
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Content-Type": "application/json",
            "x-nd-authorization": f"Bearer {token}",
            "User-Agent": "AuraHub/1.0",
        }

        req = urllib.request.Request(url, data=payload, headers=headers, method=method.upper())

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_text = resp.read().decode("utf-8")
                data = json.loads(resp_text) if resp_text else {}
                return True, data
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # Token might have expired; retry once with a refreshed token
                refreshed_token = self._get_native_token(force_refresh=True)
                if refreshed_token:
                    headers["x-nd-authorization"] = f"Bearer {refreshed_token}"
                    retry_req = urllib.request.Request(url, data=payload, headers=headers, method=method.upper())
                    try:
                        with urllib.request.urlopen(retry_req, timeout=10) as retry_resp:
                            resp_text = retry_resp.read().decode("utf-8")
                            return True, json.loads(resp_text) if resp_text else {}
                    except Exception:
                        pass
            return False, f"Navidrome HTTP Error {e.code}: {e.reason}"
        except Exception as e:
            return False, f"Native API error: {e}"

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

    def get_now_playing(self) -> Dict[str, Any]:
        """Retrieves active playback sessions across the Navidrome instance (/rest/getNowPlaying).

        Returns:
            Dict containing:
                ok (bool): True if successful.
                entries (list): List of parsed active playback session dicts.
                streams (list): Alias to entries list.
                count (int): Number of active streams.
                message (str, optional): Error message if ok is False.
        """
        res = self._request("getNowPlaying", {"f": "json"})
        if not res.get("ok"):
            return {
                "ok": False,
                "entries": [],
                "streams": [],
                "count": 0,
                "message": res.get("message", "Failed to retrieve now playing data."),
            }

        sub_resp = res.get("data", {})
        now_playing = sub_resp.get("nowPlaying", {})
        raw_entries = now_playing.get("entry", [])

        if isinstance(raw_entries, dict):
            entry_list = [raw_entries]
        elif isinstance(raw_entries, list):
            entry_list = raw_entries
        else:
            entry_list = []

        parsed_entries = []
        for entry in entry_list:
            if not isinstance(entry, dict):
                continue
            username = entry.get("username", "Unknown User")
            title = entry.get("title", "Unknown Title")
            artist = entry.get("artist", "Unknown Artist")
            album = entry.get("album", "Unknown Album")
            player = (
                entry.get("playerName")
                or entry.get("clientName")
                or entry.get("player")
                or "Unknown Client"
            )
            bitrate = entry.get("bitRate")
            suffix = entry.get("suffix") or ""
            minutes_ago = entry.get("minutesAgo", 0)
            duration = entry.get("duration", 0)

            parsed_entries.append(
                {
                    "username": username,
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "player": player,
                    "playerName": player,
                    "bitrate": bitrate,
                    "bitRate": bitrate,
                    "format": suffix.upper() if suffix else "MP3",
                    "suffix": suffix,
                    "minutes_ago": minutes_ago,
                    "minutesAgo": minutes_ago,
                    "duration": duration,
                    "entry_id": str(entry.get("id", "")),
                }
            )

        return {
            "ok": True,
            "entries": parsed_entries,
            "streams": parsed_entries,
            "count": len(parsed_entries),
        }

    def get_users(self) -> Dict[str, Any]:
        """Fetches list of all users from Navidrome (uses Native API first, Subsonic fallback)."""
        # 1. Try Navidrome native REST API (authoritative full user list)
        ok, native_users = self._native_request("GET", "user")
        if ok and isinstance(native_users, list):
            users: List[Dict[str, Any]] = []
            for u in native_users:
                uname = u.get("userName") or u.get("name") or "Unknown"
                users.append(
                    {
                        "id": u.get("id"),
                        "username": uname,
                        "name": u.get("name", uname),
                        "email": u.get("email", ""),
                        "adminRole": bool(u.get("isAdmin")),
                        "streamRole": True,
                        "downloadRole": True,
                        "lastLoginAt": u.get("lastLoginAt"),
                    }
                )
            return {"ok": True, "users": users}

        # 2. Subsonic fallback
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
        """Fetches details for a specific user (uses Native API first, Subsonic fallback)."""
        all_res = self.get_users()
        if all_res.get("ok"):
            for u in all_res.get("users", []):
                if u.get("username", "").lower() == username.lower():
                    return {"ok": True, "user": u}

        # Subsonic fallback
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
        """Creates a new user account in Navidrome (uses Native API first, Subsonic fallback)."""
        payload = {
            "userName": username,
            "name": username,
            "password": password,
            "email": email,
            "isAdmin": admin_role,
        }
        ok, resp = self._native_request("POST", "user", payload)
        if ok:
            return {"ok": True, "message": f"User '{username}' created successfully."}

        # Subsonic fallback
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
        new_username: Optional[str] = None,
        password: Optional[str] = None,
        email: Optional[str] = None,
        admin_role: Optional[bool] = None,
        stream_role: Optional[bool] = None,
        download_role: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Updates an existing user account in Navidrome (uses Native API first, Subsonic fallback)."""
        # Look up user ID
        user_res = self.get_user(username)
        user_id = user_res.get("user", {}).get("id") if user_res.get("ok") else None

        if user_id:
            payload: Dict[str, Any] = {}
            if new_username is not None:
                payload["userName"] = new_username
                payload["name"] = new_username
            if password is not None:
                payload["password"] = password
            if email is not None:
                payload["email"] = email
            if admin_role is not None:
                payload["isAdmin"] = admin_role

            ok, resp = self._native_request("PUT", f"user/{user_id}", payload)
            if ok:
                display_name = new_username or username
                return {"ok": True, "message": f"User '{display_name}' updated successfully."}

        # Subsonic fallback
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
        """Deletes a user account from Navidrome (uses Native API first, Subsonic fallback)."""
        # Look up user ID
        user_res = self.get_user(username)
        user_id = user_res.get("user", {}).get("id") if user_res.get("ok") else None

        if user_id:
            ok, resp = self._native_request("DELETE", f"user/{user_id}")
            if ok:
                return {"ok": True, "message": f"User '{username}' deleted successfully."}

        # Subsonic fallback
        res = self._request("deleteUser", {"username": username})
        if res.get("ok"):
            return {
                "ok": True,
                "message": f"User '{username}' deleted successfully.",
            }
        return res


# Global singleton instance initialized from config
navidrome_client = NavidromeClient()
