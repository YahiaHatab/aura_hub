"""Persistent JSON storage and management for Aura Hub music ingestion requests."""

import json
import logging
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from services.downloader import executor, run_pipeline, run_youtube_search
from services.navidrome import navidrome_client

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REQUESTS_FILE = DATA_DIR / "requests.json"

_file_lock = threading.RLock()


def _ensure_storage_exists():
    """Initializes the data directory and requests.json if not present."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not REQUESTS_FILE.exists():
        with open(REQUESTS_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)


def load_all_requests() -> List[Dict[str, Any]]:
    """Loads all requests from persistent storage."""
    _ensure_storage_exists()
    with _file_lock:
        try:
            with open(REQUESTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
                return []
        except Exception as e:
            logger.error(f"Error reading requests from {REQUESTS_FILE}: {e}")
            return []


def save_all_requests(requests: List[Dict[str, Any]]) -> bool:
    """Saves requests list to persistent storage."""
    _ensure_storage_exists()
    with _file_lock:
        try:
            with open(REQUESTS_FILE, "w", encoding="utf-8") as f:
                json.dump(requests, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logger.error(f"Error saving requests to {REQUESTS_FILE}: {e}")
            return False


def create_request(
    user_id: int, user_name: str, query_or_url: str, req_id: Optional[str] = None
) -> Dict[str, Any]:
    """Creates a new PENDING request and appends it to storage."""
    rid = req_id or secrets.token_hex(4)
    item = {
        "id": rid,
        "user_id": user_id,
        "user_name": user_name or f"User {user_id}",
        "query_or_url": query_or_url.strip(),
        "status": "PENDING",
        "created_at": time.time(),
        "admin_name": None,
        "completed_at": None,
        "error": None,
    }

    requests = load_all_requests()
    requests.insert(0, item)
    save_all_requests(requests)
    return item


def update_request_status(
    req_id: str,
    status: str,
    admin_name: Optional[str] = None,
    error: Optional[str] = None,
) -> bool:
    """Updates the status and metadata of an existing request in storage."""
    requests = load_all_requests()
    for r in requests:
        if r["id"] == req_id:
            r["status"] = status
            if admin_name:
                r["admin_name"] = admin_name
            if error:
                r["error"] = error
            if status in ("COMPLETED", "REJECTED", "FAILED"):
                r["completed_at"] = time.time()
            save_all_requests(requests)
            return True
    return False


def get_requests_for_user(user_id: int, is_admin: bool = False) -> List[Dict[str, Any]]:
    """Returns all requests for an administrator, or only submitted requests for regular users."""
    all_reqs = load_all_requests()
    if is_admin:
        return all_reqs
    return [r for r in all_reqs if r.get("user_id") == user_id]


def send_telegram_notification(chat_id: int, text: str):
    """Sends a Telegram message to a user using the Bot HTTP API."""
    if not config.TELEGRAM_BOT_TOKEN or not chat_id:
        return

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}

    try:
        import httpx

        with httpx.Client(timeout=10.0) as client:
            client.post(url, json=payload)
    except Exception as e:
        logger.warning(f"Failed to dispatch Telegram message to user {chat_id}: {e}")


def _execute_approval_pipeline(req_id: str, admin_name: str):
    """Background worker executing search, download, tagging, and library scan for an approved request."""
    requests = load_all_requests()
    target_req = next((r for r in requests if r["id"] == req_id), None)
    if not target_req:
        return

    target_req["status"] = "DOWNLOADING"
    save_all_requests(requests)

    query_or_url = target_req["query_or_url"]
    requester_id = target_req["user_id"]

    try:
        media_url = query_or_url
        if not re.match(r"^https?://", media_url.strip()):
            search_results = run_youtube_search(media_url, limit=1)
            if not search_results:
                raise RuntimeError(f"No results found on YouTube matching '{media_url}'")
            media_url = search_results[0]["url"]

        target_folder, meta = run_pipeline(media_url)

        # Trigger Navidrome scan
        if navidrome_client.is_configured():
            navidrome_client.start_scan()

        album_name = meta.get("album", "Requested Album")
        artist_name = meta.get("artist", "Artist")

        # Mark request completed
        reqs = load_all_requests()
        for r in reqs:
            if r["id"] == req_id:
                r["status"] = "COMPLETED"
                r["completed_at"] = time.time()
                r["admin_name"] = admin_name
                break
        save_all_requests(reqs)

        # Notify requester
        completion_msg = (
            "🎉 *Your Music Request is Live!*\n\n"
            f"💿 *{album_name}* — *{artist_name}*\n"
            "✨ Ingestion and library indexing completed. Ready in Navidrome & Symfonium!"
        )
        send_telegram_notification(requester_id, completion_msg)

    except Exception as e:
        logger.exception(f"Request {req_id} pipeline execution failed: {e}")
        reqs = load_all_requests()
        for r in reqs:
            if r["id"] == req_id:
                r["status"] = "FAILED"
                r["error"] = str(e)
                r["completed_at"] = time.time()
                break
        save_all_requests(reqs)

        fail_msg = (
            f"❌ *Music Request Failed*\n\n"
            f"We could not ingest your request for `{query_or_url}`.\n"
            f"Reason: {e}"
        )
        send_telegram_notification(requester_id, fail_msg)


def handle_request_action(
    req_id: str, action: str, admin_user: Dict[str, Any]
) -> Tuple[bool, str]:
    """Handles an administrative action (approve / reject) on a request."""
    requests = load_all_requests()
    target_req = next((r for r in requests if r["id"] == req_id), None)
    if not target_req:
        return False, f"Request '{req_id}' not found."

    admin_name = admin_user.get("first_name") or admin_user.get("username") or "Admin"
    requester_id = target_req["user_id"]
    query_or_url = target_req["query_or_url"]

    if action.lower() == "approve":
        if target_req["status"] in ("APPROVED", "DOWNLOADING", "COMPLETED"):
            return False, f"Request is already {target_req['status']}."

        target_req["status"] = "APPROVED"
        target_req["admin_name"] = admin_name
        save_all_requests(requests)

        # Notify requester
        notify_msg = (
            "✅ *Music Request Approved!*\n\n"
            f"Your request for `{query_or_url}` was approved by {admin_name}.\n"
            "The download and metadata tagging pipeline has started!"
        )
        send_telegram_notification(requester_id, notify_msg)

        # Spawn pipeline in executor
        executor.submit(_execute_approval_pipeline, req_id, admin_name)
        return True, "Request approved. Ingestion pipeline started."

    elif action.lower() == "reject":
        target_req["status"] = "REJECTED"
        target_req["admin_name"] = admin_name
        target_req["completed_at"] = time.time()
        save_all_requests(requests)
        reject_msg = (
            "❌ *Music Request Declined*\n\n"
            f"Your request for `{query_or_url}` was declined by an administrator."
        )
        send_telegram_notification(requester_id, reject_msg)
        return True, "Request rejected."

    return False, f"Unknown action '{action}'."


def clear_completed_requests(preserve_active: bool = True) -> int:
    """Clears completed, rejected, and failed requests from storage.

    If preserve_active is True, retains requests with status PENDING, APPROVED, or DOWNLOADING.
    Returns the count of requests removed.
    """
    _ensure_storage_exists()
    with _file_lock:
        requests = load_all_requests()
        if preserve_active:
            active_statuses = {"PENDING", "APPROVED", "DOWNLOADING"}
            remaining = [r for r in requests if r.get("status") in active_statuses]
        else:
            remaining = []

        cleared_count = len(requests) - len(remaining)
        save_all_requests(remaining)
        return cleared_count

