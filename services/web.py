"""FastAPI Web Server and Telegram Mini App backend for Aura Hub.

Provides authenticated REST API endpoints and dashboard UI for managing
Navidrome users, monitoring live playback, and triggering server actions.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from services.navidrome import navidrome_client
from services.system import get_album_folders, get_disk_metrics, get_system_diagnostic_summary

logger = logging.getLogger(__name__)

# Root static directory for frontend assets
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Aura Hub Dashboard",
    description="Telegram Mini App and Management API for Aura Hub & Navidrome",
    version="1.0.0",
)

# Enable CORS for Telegram WebApp environment and reverse proxy
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_telegram_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int = 86400,
) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """Validates Telegram WebApp initData string using HMAC-SHA256 signature verification.

    Algorithm:
    1. Parse query string and extract received 'hash'.
    2. Sort key-value pairs alphabetically (excluding 'hash') and join with linebreaks.
    3. Generate secret_key = HMAC_SHA256(b"WebAppData", bot_token).
    4. Calculate expected_hash = HMAC_SHA256(secret_key, data_check_string).
    5. Compare hashes, verify auth_date freshness, and parse user JSON.

    Returns:
        (is_valid, user_data_dict, error_message)
    """
    if not init_data:
        return False, None, "Missing Telegram initData"

    try:
        parsed_qsl = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
        data_dict = dict(parsed_qsl)
    except Exception as e:
        return False, None, f"Failed to parse initData query string: {e}"

    received_hash = data_dict.get("hash")
    if not received_hash:
        return False, None, "Missing signature hash parameter"

    # Build data_check_string from sorted pairs
    pairs = [f"{k}={v}" for k, v in data_dict.items() if k != "hash"]
    pairs.sort()
    data_check_string = "\n".join(pairs)

    # 1. secret_key = HMAC_SHA256("WebAppData", bot_token)
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()

    # 2. calculated_hash = HMAC_SHA256(secret_key, data_check_string)
    calculated_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        return False, None, "Invalid Telegram HMAC signature"

    # 3. Freshness check
    auth_date_str = data_dict.get("auth_date")
    if auth_date_str:
        try:
            auth_date = int(auth_date_str)
            if time.time() - auth_date > max_age_seconds:
                return False, None, "Telegram session expired (auth_date too old)"
        except ValueError:
            return False, None, "Invalid auth_date format"

    # 4. User data extraction
    user_json = data_dict.get("user")
    if not user_json:
        return False, None, "Missing user field in initData"

    try:
        user_data = json.loads(user_json)
    except Exception as e:
        return False, None, f"Invalid user JSON payload: {e}"

    return True, user_data, ""


async def verify_admin_user(
    authorization: Optional[str] = Header(None),
    x_telegram_init_data: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Dependency that ensures requests are signed by an authorized Telegram administrator."""
    raw_token = ""
    if authorization:
        if authorization.startswith("Bearer "):
            raw_token = authorization[7:].strip()
        else:
            raw_token = authorization.strip()
    elif x_telegram_init_data:
        raw_token = x_telegram_init_data.strip()

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header containing Telegram initData.",
        )

    is_valid, user_data, err_msg = verify_telegram_init_data(
        raw_token, config.TELEGRAM_BOT_TOKEN
    )

    if not is_valid or not user_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Telegram authentication failed: {err_msg}",
        )

    user_id = user_data.get("id")
    if not user_id or user_id not in config.ADMIN_USER_IDS:
        logger.warning(
            f"Unauthorized WebApp access attempt by user {user_id} ({user_data.get('username')})"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Administrative privileges required to access Aura Hub WebApp.",
        )

    return user_data


# ================= DATA MODELS =================
class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=4, max_length=100)
    email: Optional[str] = ""
    admin_role: bool = False


class UserActionRequest(BaseModel):
    username: str = Field(..., min_length=1)


# ================= STATIC & DASHBOARD ROUTES =================
@app.get("/", response_class=FileResponse)
@app.get("/webapp", response_class=FileResponse)
@app.get("/hub", response_class=FileResponse)
@app.get("/hub/", response_class=FileResponse)
@app.get("/hub/webapp", response_class=FileResponse)
async def serve_dashboard():
    """Serves the Telegram Mini App single-page dashboard."""
    index_file = STATIC_DIR / "index.html"
    if not index_file.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dashboard frontend not found. Ensure static/index.html exists.",
        )
    return FileResponse(index_file)


# Mount static directory for JS/CSS assets under both root and /hub prefixes
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.mount("/hub/static", StaticFiles(directory=str(STATIC_DIR)), name="hub_static")


# ================= REST API ROUTER =================
api_router = APIRouter()


@api_router.get("/users")
async def get_users_api(admin_user: Dict[str, Any] = Depends(verify_admin_user)):
    """Fetches list of registered Navidrome users with roles and status."""
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, navidrome_client.get_users)
    return res


@api_router.post("/users/create")
async def create_user_api(
    payload: CreateUserRequest,
    admin_user: Dict[str, Any] = Depends(verify_admin_user),
):
    """Creates a new Navidrome account."""
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        None,
        lambda: navidrome_client.create_user(
            username=payload.username.strip(),
            password=payload.password.strip(),
            email=payload.email.strip() if payload.email else "",
            admin_role=payload.admin_role,
        ),
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message", "User creation failed"))
    return res


@api_router.post("/users/delete")
async def delete_user_api(
    payload: UserActionRequest,
    admin_user: Dict[str, Any] = Depends(verify_admin_user),
):
    """Permanently deletes a Navidrome account."""
    username = payload.username.strip()
    if config.NAVIDROME_USER and username.lower() == config.NAVIDROME_USER.lower():
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the primary configured server administrator account.",
        )

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, lambda: navidrome_client.delete_user(username))
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message", "User deletion failed"))
    return res


@api_router.post("/users/reset-password")
async def reset_password_api(
    payload: UserActionRequest,
    admin_user: Dict[str, Any] = Depends(verify_admin_user),
):
    """Generates a secure random password for the specified user and updates Navidrome."""
    username = payload.username.strip()
    new_password = secrets.token_urlsafe(10)

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        None, lambda: navidrome_client.update_user(username, password=new_password)
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message", "Password reset failed"))

    return {
        "ok": True,
        "username": username,
        "password": new_password,
        "message": f"Password for {username} successfully updated.",
    }


@api_router.get("/nowplaying")
async def get_now_playing_api(admin_user: Dict[str, Any] = Depends(verify_admin_user)):
    """Returns active playback sessions on Navidrome."""
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, navidrome_client.get_now_playing)
    streams = res.get("streams") or res.get("entries") or []
    return {
        "ok": res.get("ok", True),
        "streams": streams,
        "entries": streams,
        "count": len(streams),
    }


@api_router.post("/rescan")
async def trigger_rescan_api(admin_user: Dict[str, Any] = Depends(verify_admin_user)):
    """Triggers an instant Subsonic library scan on Navidrome."""
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, navidrome_client.start_scan)
    return res


@api_router.get("/system")
async def get_system_api(admin_user: Dict[str, Any] = Depends(verify_admin_user)):
    """Returns storage metrics, indexed media counts, and system diagnostics."""
    loop = asyncio.get_running_loop()

    def _collect():
        disk = get_disk_metrics()
        diagnostics = get_system_diagnostic_summary()

        # Count indexed mp3 and lrc files safely
        mp3_count = 0
        lrc_count = 0
        if config.BASE_DOWNLOAD_DIR.is_dir():
            for p in config.BASE_DOWNLOAD_DIR.rglob("*"):
                if p.is_file():
                    s = p.suffix.lower()
                    if s == ".mp3":
                        mp3_count += 1
                    elif s == ".lrc":
                        lrc_count += 1

        folders = get_album_folders()

        return {
            "ok": True,
            "disk": disk,
            "mp3_count": mp3_count,
            "lrc_count": lrc_count,
            "album_count": len(folders),
            "diagnostics": diagnostics,
            "navidrome_connected": navidrome_client.is_configured(),
        }

    data = await loop.run_in_executor(None, _collect)
    return data


# Mount API routes under both /api and /hub/api to support reverse proxy subpaths
app.include_router(api_router, prefix="/api")
app.include_router(api_router, prefix="/hub/api")

