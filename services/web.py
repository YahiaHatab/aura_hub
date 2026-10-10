"""FastAPI Web Server and Telegram Mini App backend for Aura Hub.

Provides authenticated REST API endpoints and dashboard UI for managing
Navidrome users, monitoring live playback, queuing requests, direct downloading,
and auditing library metadata.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from services.library_browser import (
    DEFAULT_PLACEHOLDER_SVG,
    get_library_albums,
    refetch_album_lyrics,
    remove_album,
    resolve_album_cover,
)
from services.metadata import (
    UnifiedAlbumMetadata,
    UnifiedTrackMetadata,
    fetch_lrclib_lyrics_async,
    search_album_metadata_candidates_async,
    search_metadata_async,
    search_track_metadata_candidates_async,
)
import services.navidrome
from services.navidrome import navidrome_client
from services.requests import (
    clear_completed_requests,
    create_request,
    get_requests_for_user,
    handle_request_action,
)
from services.system import get_album_folders, get_disk_metrics, get_system_diagnostic_summary
from services.tagger import (
    apply_unified_metadata_to_album,
    apply_unified_metadata_to_file,
    extract_cover_bytes,
    read_tags,
    write_loose_cover,
    write_tags,
)
from services.tasks import get_all_tasks, start_download_task

logger = logging.getLogger(__name__)

# Root static directory for frontend assets
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Aura Hub Dashboard",
    description="Telegram Mini App and Management API for Aura Hub & Navidrome",
    version="1.1.0",
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


async def get_current_user(
    authorization: Optional[str] = Header(None),
    x_telegram_init_data: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Authenticates the incoming request using Telegram initData from Bearer or custom header."""
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
    user_data["is_admin"] = bool(user_id and user_id in config.ADMIN_USER_IDS)
    user_data["is_allowed"] = bool(
        user_id and (user_id in config.ADMIN_USER_IDS or user_id in config.ALLOWED_USER_IDS)
    )
    return user_data


async def verify_authorized_user(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """Ensures the requester is in ALLOWED_USER_IDS or ADMIN_USER_IDS."""
    if not current_user.get("is_allowed"):
        logger.warning(
            f"Unauthorized WebApp access attempt by user {current_user.get('id')} ({current_user.get('username')})"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You are not authorized to access Aura Hub.",
        )
    return current_user


async def verify_admin_user(
    current_user: Dict[str, Any] = Depends(verify_authorized_user),
) -> Dict[str, Any]:
    """Ensures the requester has full administrative privileges (ADMIN_USER_IDS)."""
    if not current_user.get("is_admin"):
        logger.warning(
            f"Non-admin WebApp access attempt by user {current_user.get('id')} ({current_user.get('username')})"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Administrative privileges required to access Aura Hub WebApp.",
        )
    return current_user


# ================= DATA MODELS =================
class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=4, max_length=100)
    email: Optional[str] = ""
    admin_role: bool = False


class UserActionRequest(BaseModel):
    username: str = Field(..., min_length=1)


class DownloadRequest(BaseModel):
    url: str = Field(..., min_length=1)


class SubmitRequestPayload(BaseModel):
    query_or_url: str = Field(..., min_length=1)


class RequestActionPayload(BaseModel):
    request_id: str = Field(..., min_length=1)
    action: str = Field(..., min_length=1)


class FolderActionPayload(BaseModel):
    folder: str = Field(..., min_length=1)


class ClearRequestsPayload(BaseModel):
    status: Optional[str] = "completed_only"


class ApplyMetadataPayload(BaseModel):
    path: str = Field(..., min_length=1)
    type: Optional[str] = "album"  # "album" or "track"
    candidate: Optional[Dict[str, Any]] = None
    album_data: Optional[Dict[str, Any]] = None
    track_data: Optional[Dict[str, Any]] = None
    rescan: Optional[bool] = True


class CommitTagsPayload(BaseModel):
    path: str = Field(..., min_length=1)
    fields: Dict[str, Any] = Field(default_factory=dict)
    lyrics_lrc: Optional[str] = None
    cover_data_base64: Optional[str] = None
    cover_url: Optional[str] = None
    rescan: Optional[bool] = True



def resolve_safe_path(rel_or_abs_path: str) -> Path:
    """Safely resolves an album folder or audio file path within the music library."""
    base = config.BASE_DOWNLOAD_DIR.resolve()
    cleaned = rel_or_abs_path.strip().lstrip("/\\")
    target = (base / cleaned).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        abs_target = Path(rel_or_abs_path).resolve()
        try:
            abs_target.relative_to(base)
            target = abs_target
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Path traversal forbidden: Target path is outside music library.",
            )
    if not target.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Target path not found: {cleaned}",
        )
    return target


def inspect_audio_file(file_path: Path) -> Dict[str, Any]:
    """Inspects Vorbis, ID3, or MP4 tags and lyrics of an individual audio track."""
    ext = file_path.suffix.lower()
    info = {
        "filename": file_path.name,
        "format": ext.lstrip("."),
        "title": file_path.stem,
        "artist": "",
        "album_artist": "",
        "album": "",
        "year": "",
        "genre": "",
        "track_number": 1,
        "total_tracks": 1,
        "disc_number": 1,
        "duration_seconds": 0.0,
        "composers": [],
        "producers": [],
        "has_embedded_cover": False,
        "has_lyrics": False,
        "has_lrc": file_path.with_suffix(".lrc").is_file(),
    }

    try:
        mut_file = mutagen.File(str(file_path))
        if mut_file and mut_file.info:
            info["duration_seconds"] = round(float(mut_file.info.length), 1)
    except Exception:
        pass

    try:
        if ext == ".mp3":
            try:
                id3 = ID3(str(file_path))
                if "TIT2" in id3: info["title"] = str(id3["TIT2"].text[0])
                if "TPE1" in id3: info["artist"] = str(id3["TPE1"].text[0])
                if "TPE2" in id3: info["album_artist"] = str(id3["TPE2"].text[0])
                if "TALB" in id3: info["album"] = str(id3["TALB"].text[0])
                if "TDRC" in id3: info["year"] = str(id3["TDRC"].text[0])
                if "TCON" in id3: info["genre"] = str(id3["TCON"].text[0])
                if "TRCK" in id3:
                    trck = str(id3["TRCK"].text[0])
                    if "/" in trck:
                        p = trck.split("/", 1)
                        if p[0].isdigit(): info["track_number"] = int(p[0])
                        if p[1].isdigit(): info["total_tracks"] = int(p[1])
                    elif trck.isdigit():
                        info["track_number"] = int(trck)
                if "TCOM" in id3:
                    info["composers"] = [c.strip() for c in str(id3["TCOM"].text[0]).split(",") if c.strip()]
                for txxx in id3.getall("TXXX"):
                    if txxx.desc.upper() == "PRODUCER":
                        info["producers"] = [p.strip() for p in str(txxx.text[0]).split(",") if p.strip()]
                info["has_embedded_cover"] = bool(id3.getall("APIC"))
                info["has_lyrics"] = bool(id3.getall("USLT"))
            except ID3NoHeaderError:
                pass
        elif ext == ".flac":
            fl = FLAC(str(file_path))
            if "title" in fl and fl["title"]: info["title"] = fl["title"][0]
            if "artist" in fl and fl["artist"]: info["artist"] = fl["artist"][0]
            if "albumartist" in fl and fl["albumartist"]: info["album_artist"] = fl["albumartist"][0]
            if "album" in fl and fl["album"]: info["album"] = fl["album"][0]
            if "date" in fl and fl["date"]: info["year"] = fl["date"][0][:4]
            if "genre" in fl and fl["genre"]: info["genre"] = fl["genre"][0]
            if "tracknumber" in fl and fl["tracknumber"] and fl["tracknumber"][0].isdigit():
                info["track_number"] = int(fl["tracknumber"][0])
            if "totaltracks" in fl and fl["totaltracks"] and fl["totaltracks"][0].isdigit():
                info["total_tracks"] = int(fl["totaltracks"][0])
            if "composer" in fl: info["composers"] = fl["composer"]
            if "producer" in fl: info["producers"] = fl["producer"]
            info["has_embedded_cover"] = bool(fl.pictures)
            info["has_lyrics"] = "lyrics" in fl
        elif ext == ".opus":
            op = OggOpus(str(file_path))
            if "title" in op and op["title"]: info["title"] = op["title"][0]
            if "artist" in op and op["artist"]: info["artist"] = op["artist"][0]
            if "albumartist" in op and op["albumartist"]: info["album_artist"] = op["albumartist"][0]
            if "album" in op and op["album"]: info["album"] = op["album"][0]
            if "date" in op and op["date"]: info["year"] = op["date"][0][:4]
            if "genre" in op and op["genre"]: info["genre"] = op["genre"][0]
            if "tracknumber" in op and op["tracknumber"] and op["tracknumber"][0].isdigit():
                info["track_number"] = int(op["tracknumber"][0])
            if "totaltracks" in op and op["totaltracks"] and op["totaltracks"][0].isdigit():
                info["total_tracks"] = int(op["totaltracks"][0])
            if "composer" in op: info["composers"] = op["composer"]
            if "producer" in op: info["producers"] = op["producer"]
            info["has_embedded_cover"] = "metadata_block_picture" in op
            info["has_lyrics"] = "lyrics" in op
        elif ext == ".m4a":
            mp = MP4(str(file_path))
            if "\xa9nam" in mp and mp["\xa9nam"]: info["title"] = mp["\xa9nam"][0]
            if "\xa9ART" in mp and mp["\xa9ART"]: info["artist"] = mp["\xa9ART"][0]
            if "aART" in mp and mp["aART"]: info["album_artist"] = mp["aART"][0]
            if "\xa9alb" in mp and mp["\xa9alb"]: info["album"] = mp["\xa9alb"][0]
            if "\xa9day" in mp and mp["\xa9day"]: info["year"] = str(mp["\xa9day"][0])[:4]
            if "\xa9gen" in mp and mp["\xa9gen"]: info["genre"] = mp["\xa9gen"][0]
            if "trkn" in mp and mp["trkn"]:
                info["track_number"] = mp["trkn"][0][0]
                info["total_tracks"] = mp["trkn"][0][1]
            if "\xa9wrt" in mp and mp["\xa9wrt"]: info["composers"] = [mp["\xa9wrt"][0]]
            info["has_embedded_cover"] = "covr" in mp
            info["has_lyrics"] = "\xa9lyr" in mp
    except Exception as e:
        logger.debug(f"Error reading tags from {file_path.name}: {e}")

    return info


def inspect_folder(folder_path: Path) -> Dict[str, Any]:
    """Inspects audio files in an album folder to produce an aggregated metadata overview."""
    valid_exts = {".mp3", ".flac", ".opus", ".m4a"}
    audio_files = sorted(
        [p for p in folder_path.glob("*") if p.is_file() and p.suffix.lower() in valid_exts],
        key=lambda x: x.name,
    )
    inspected_tracks = [inspect_audio_file(f) for f in audio_files]

    album_name = folder_path.name
    artist_name = folder_path.parent.name if folder_path.parent != config.BASE_DOWNLOAD_DIR else ""
    album_artist_name = artist_name
    year = ""
    genre = ""

    for t in inspected_tracks:
        if t["album"] and not album_name:
            album_name = t["album"]
        if t["artist"] and not artist_name:
            artist_name = t["artist"]
        if t["album_artist"] and not album_artist_name:
            album_artist_name = t["album_artist"]
        if t["year"] and not year:
            year = t["year"]
        if t["genre"] and not genre:
            genre = t["genre"]

    has_loose_cover = False
    for cov_name in ("cover.jpg", "cover.png", "folder.jpg", "folder.png"):
        if (folder_path / cov_name).is_file():
            has_loose_cover = True
            break

    lrc_count = sum(1 for t in inspected_tracks if t["has_lrc"])

    return {
        "folder": str(folder_path.relative_to(config.BASE_DOWNLOAD_DIR)),
        "album": album_name,
        "artist": artist_name,
        "album_artist": album_artist_name or artist_name,
        "year": year,
        "genre": genre,
        "track_count": len(inspected_tracks),
        "has_cover": has_loose_cover or any(t["has_embedded_cover"] for t in inspected_tracks),
        "lrc_count": lrc_count,
        "tracks": inspected_tracks,
    }


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
    return FileResponse(
        index_file,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# Mount static directory for JS/CSS assets under both root and /hub prefixes
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.mount("/hub/static", StaticFiles(directory=str(STATIC_DIR)), name="hub_static")


# ================= REST API ROUTER =================
api_router = APIRouter()


@api_router.get("/me")
async def get_me_api(user: Dict[str, Any] = Depends(verify_authorized_user)):
    """Returns current user's profile and administrative role information."""
    return {
        "ok": True,
        "user_id": user.get("id"),
        "username": user.get("username", ""),
        "first_name": user.get("first_name", ""),
        "is_admin": user.get("is_admin", False),
    }


# ----- USERS API -----
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


# ----- NOW PLAYING API -----
@api_router.get("/nowplaying")
async def get_now_playing_api(user: Dict[str, Any] = Depends(verify_authorized_user)):
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


# ----- IN-APP DOWNLOADER & TASK POLLING API -----
@api_router.post("/download")
async def trigger_download_api(
    payload: DownloadRequest,
    admin_user: Dict[str, Any] = Depends(verify_admin_user),
):
    """Spawns direct ingestion pipeline and registers background task."""
    admin_name = admin_user.get("first_name") or admin_user.get("username") or "Admin"
    task = start_download_task(payload.url.strip(), started_by=admin_name)
    return {"ok": True, "task": task}


@api_router.get("/tasks")
async def get_tasks_api(admin_user: Dict[str, Any] = Depends(verify_admin_user)):
    """Returns active and recent background download tasks."""
    tasks = get_all_tasks()
    return {"ok": True, "tasks": tasks}


# ----- REQUEST QUEUE API -----
@api_router.post("/requests/submit")
async def submit_request_api(
    payload: SubmitRequestPayload,
    user: Dict[str, Any] = Depends(verify_authorized_user),
):
    """Queues a new music request from an authorized user or admin."""
    user_id = user.get("id")
    user_name = user.get("username") or user.get("first_name") or f"User {user_id}"
    req = create_request(user_id=user_id, user_name=user_name, query_or_url=payload.query_or_url)
    return {"ok": True, "request": req}


@api_router.get("/requests")
async def get_requests_api(user: Dict[str, Any] = Depends(verify_authorized_user)):
    """Fetches music requests (all requests for admins, submitted requests for regular users)."""
    reqs = get_requests_for_user(
        user_id=user.get("id"),
        is_admin=user.get("is_admin", False),
    )
    return {"ok": True, "requests": reqs}


@api_router.post("/requests/action")
async def action_request_api(
    payload: RequestActionPayload,
    admin_user: Dict[str, Any] = Depends(verify_admin_user),
):
    """Approve or reject a music request (Admin only)."""
    ok, msg = handle_request_action(
        req_id=payload.request_id.strip(),
        action=payload.action.strip(),
        admin_user=admin_user,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


@api_router.post("/requests/clear")
async def clear_requests_api(
    payload: Optional[ClearRequestsPayload] = None,
    admin_user: Dict[str, Any] = Depends(verify_admin_user),
):
    """Clears past resolved/completed/rejected requests while preserving pending/active ones."""
    filter_mode = payload.status if payload and payload.status else "completed_only"
    preserve_active = (filter_mode == "completed_only")
    cleared_count = clear_completed_requests(preserve_active=preserve_active)
    return {
        "ok": True,
        "cleared": cleared_count,
        "message": f"Successfully cleared {cleared_count} past requests.",
    }


# ----- VISUAL LIBRARY BROWSER API -----
@api_router.get("/library")
async def get_library_api(user: Dict[str, Any] = Depends(verify_authorized_user)):
    """Returns indexed album folders with cover presence and lyrics sync status."""
    loop = asyncio.get_running_loop()
    albums = await loop.run_in_executor(None, get_library_albums)
    return {"ok": True, "albums": albums, "count": len(albums)}


@api_router.get("/cover")
async def get_cover_api(path: str):
    """Safely serves album cover artwork with path traversal verification and fallback."""
    loop = asyncio.get_running_loop()
    try:
        cover_path = await loop.run_in_executor(None, lambda: resolve_album_cover(path))
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal forbidden.")

    if cover_path and cover_path.is_file():
        media_type = "image/jpeg"
        if cover_path.suffix.lower() == ".png":
            media_type = "image/png"
        elif cover_path.suffix.lower() == ".webp":
            media_type = "image/webp"
        return FileResponse(cover_path, media_type=media_type)

    return Response(content=DEFAULT_PLACEHOLDER_SVG, media_type="image/svg+xml")


@api_router.post("/library/refetch-lyrics")
async def refetch_lyrics_api(
    payload: FolderActionPayload,
    admin_user: Dict[str, Any] = Depends(verify_admin_user),
):
    """Refetches and syncs lyrics for an album folder."""
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, lambda: refetch_album_lyrics(payload.folder))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid folder path.")

    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message", "Lyrics sync failed."))
    return res


@api_router.post("/library/delete")
async def delete_album_api(
    payload: FolderActionPayload,
    admin_user: Dict[str, Any] = Depends(verify_admin_user),
):
    """Safely deletes an album folder from disk."""
    loop = asyncio.get_running_loop()
    ok, msg = await loop.run_in_executor(None, lambda: remove_album(payload.folder))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


# ----- SYSTEM & SCAN API -----
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

        audio_count = disk.get("audio_count", disk.get("mp3_count", 0))
        lrc_count = disk.get("lrc_count", 0)
        folders = get_album_folders()

        return {
            "ok": True,
            "disk": disk,
            "audio_count": audio_count,
            "mp3_count": audio_count,
            "lrc_count": lrc_count,
            "album_count": len(folders),
            "diagnostics": diagnostics,
            "navidrome_connected": navidrome_client.is_configured(),
}

    data = await loop.run_in_executor(None, _collect)
    return data


# ----- METADATA AGGREGATOR & TAG EDITOR API -----
@api_router.get("/metadata/search")
async def search_metadata_api(
    query: str,
    type: str = "album",
    artist: str = "",
    user: Dict[str, Any] = Depends(verify_authorized_user),
):
    """Queries all configured metadata backends (MusicBrainz, Deezer, iTunes, Spotify, Discogs)

    and returns ranked candidates with confidence scores and recommendation flags.
    """
    clean_q = query.strip()
    clean_art = artist.strip()
    if not clean_q:
        raise HTTPException(status_code=400, detail="Missing required query parameter.")

    if type.lower() == "track":
        candidates = await search_track_metadata_candidates_async(
            title=clean_q,
            artist=clean_art,
        )
    else:
        candidates = await search_album_metadata_candidates_async(
            album_name=clean_q,
            artist_name=clean_art,
        )

    return {
        "ok": True,
        "query": clean_q,
        "artist": clean_art,
        "type": type.lower(),
        "count": len(candidates),
        "candidates": [c.to_dict() for c in candidates],
    }


@api_router.get("/metadata/inspect")
async def inspect_metadata_api(
    path: str,
    user: Dict[str, Any] = Depends(verify_authorized_user),
):
    """Inspects existing audio metadata, ID3/Vorbis tags, artwork, and lyrics of a library item."""
    target = resolve_safe_path(path)
    loop = asyncio.get_running_loop()

    if target.is_file():
        meta = await loop.run_in_executor(None, lambda: inspect_audio_file(target))
        return {
            "ok": True,
            "path": str(target.relative_to(config.BASE_DOWNLOAD_DIR)),
            "type": "track",
            "metadata": meta,
        }
    elif target.is_dir():
        meta = await loop.run_in_executor(None, lambda: inspect_folder(target))
        return {
            "ok": True,
            "path": str(target.relative_to(config.BASE_DOWNLOAD_DIR)),
            "type": "album",
            "metadata": meta,
        }
    else:
        raise HTTPException(status_code=400, detail="Target is neither a file nor a directory.")


@api_router.post("/metadata/apply")
async def apply_metadata_api(
    payload: ApplyMetadataPayload,
    admin_user: Dict[str, Any] = Depends(verify_admin_user),
):
    """Applies user-selected candidate metadata and artwork to target album folder or track."""
    target = resolve_safe_path(payload.path)
    loop = asyncio.get_running_loop()

    is_album = target.is_dir() or payload.type == "album"

    if is_album:
        raw_album = payload.album_data or (payload.candidate.get("album_data") if payload.candidate else None)
        if not raw_album and payload.candidate:
            raw_album = payload.candidate
        if not raw_album or not isinstance(raw_album, dict):
            raise HTTPException(status_code=400, detail="Missing album metadata payload.")

        album_meta = UnifiedAlbumMetadata.from_dict(raw_album)

        # Download high-res cover bytes if URL is available
        if not album_meta.cover_bytes and album_meta.cover_url:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(album_meta.cover_url)
                    if resp.status_code == 200:
                        album_meta.cover_bytes = resp.content
            except Exception as e:
                logger.debug(f"Failed to fetch candidate cover image: {e}")

        summary = await loop.run_in_executor(
            None, lambda: apply_unified_metadata_to_album(target, album_meta)
        )

        if payload.rescan:
            asyncio.create_task(loop.run_in_executor(None, navidrome_client.start_scan))

        return {
            "ok": True,
            "message": f"Successfully applied metadata to {album_meta.album or target.name}",
            "summary": summary,
        }
    else:
        raw_track = payload.track_data or (payload.candidate.get("track_data") if payload.candidate else None)
        if not raw_track and payload.candidate:
            raw_track = payload.candidate
        if not raw_track or not isinstance(raw_track, dict):
            raise HTTPException(status_code=400, detail="Missing track metadata payload.")

        track_meta = UnifiedTrackMetadata.from_dict(raw_track)

        cover_bytes = None
        if track_meta.cover_url:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(track_meta.cover_url)
                    if resp.status_code == 200:
                        cover_bytes = resp.content
            except Exception as e:
                logger.debug(f"Failed to fetch track candidate cover: {e}")

        success = await loop.run_in_executor(
            None, lambda: apply_unified_metadata_to_file(target, track_meta, cover_bytes=cover_bytes)
        )
        if not success:
            raise HTTPException(status_code=500, detail="Failed to apply tags to audio file.")

        lrc_path = target.with_suffix(".lrc")
        if track_meta.lyrics_synced and not lrc_path.exists():
            try:
                lrc_path.write_text(track_meta.lyrics_synced, encoding="utf-8")
            except Exception:
                pass

        if payload.rescan:
            asyncio.create_task(loop.run_in_executor(None, navidrome_client.start_scan))

        return {
            "ok": True,
            "message": f"Successfully tagged {target.name}",
            "summary": track_meta.to_dict(),
        }


# =====================================================================
# UNIFIED METADATA STUDIO ENDPOINTS
# =====================================================================

@api_router.get("/tags/inspect")
async def inspect_tags_api(
    path: str,
    user: Dict[str, Any] = Depends(verify_authorized_user),
):
    """Returns current file or folder audio tags, lyrics, and artwork metadata as JSON."""
    target = resolve_safe_path(path)
    loop = asyncio.get_running_loop()
    tags = await loop.run_in_executor(None, lambda: read_tags(target))
    return {
        "ok": True,
        "path": str(target.relative_to(config.BASE_DOWNLOAD_DIR)),
        "type": "album" if target.is_dir() else "track",
        "tags": tags,
    }


@api_router.get("/tags/cover")
async def get_tags_cover_api(
    path: str,
    user: Dict[str, Any] = Depends(verify_authorized_user),
):
    """Streams current embedded artwork from audio file or folder cover art."""
    target = resolve_safe_path(path)
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, lambda: extract_cover_bytes(target))
    if res:
        cover_bytes, mime = res
        return Response(content=cover_bytes, media_type=mime)
    return Response(content=DEFAULT_PLACEHOLDER_SVG, media_type="image/svg+xml")


@api_router.get("/metadata/match")
async def match_metadata_api(
    query: str,
    type: str = "album",
    artist: str = "",
    user: Dict[str, Any] = Depends(verify_authorized_user),
):
    """Fetches provider suggestions (MusicBrainz, Deezer, Spotify, iTunes, LRCLIB) with confidence scores."""
    clean_q = query.strip()
    clean_art = artist.strip()
    if not clean_q:
        raise HTTPException(status_code=400, detail="Missing required query parameter.")

    candidates = await search_metadata_async(
        query=clean_q,
        type=type.lower(),
        artist=clean_art,
    )

    return {
        "ok": True,
        "query": clean_q,
        "artist": clean_art,
        "type": type.lower(),
        "count": len(candidates),
        "candidates": candidates,
    }


@api_router.get("/lyrics/fetch")
async def fetch_lyrics_api(
    track: str,
    artist: str = "",
    user: Dict[str, Any] = Depends(verify_authorized_user),
):
    """Quickly fetches synced and plain lyrics from LRCLIB for the Metadata Studio lyrics editor."""
    if not track.strip():
        raise HTTPException(status_code=400, detail="Missing required track parameter.")
    res = await fetch_lrclib_lyrics_async(track.strip(), artist.strip())
    return {
        "ok": True,
        "track": track.strip(),
        "artist": artist.strip(),
        "lyrics_synced": res.get("synced", ""),
        "lyrics_unsynced": res.get("plain", ""),
    }


@api_router.post("/tags/commit")
async def commit_tags_api(
    payload: CommitTagsPayload,
    admin_user: Dict[str, Any] = Depends(verify_admin_user),
):
    """Accepts edited fields, raw LRC lyrics string, and optional new cover image upload.

    Writes tags cleanly using Mutagen, writes external .lrc if provided, and triggers
    services.navidrome.scan_path() to rescan the directory.
    """
    target = resolve_safe_path(payload.path)
    loop = asyncio.get_running_loop()

    cover_bytes: Optional[bytes] = None
    if payload.cover_data_base64:
        raw_b64 = payload.cover_data_base64.strip()
        if "," in raw_b64 and "base64" in raw_b64:
            raw_b64 = raw_b64.split(",", 1)[1]
        try:
            cover_bytes = base64.b64decode(raw_b64)
        except Exception as e:
            logger.warning(f"Failed to decode base64 cover: {e}")
    elif payload.cover_url:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(payload.cover_url)
                if resp.status_code == 200:
                    cover_bytes = resp.content
        except Exception as e:
            logger.warning(f"Failed to fetch cover from URL {payload.cover_url}: {e}")

    fields = dict(payload.fields)
    if payload.lyrics_lrc is not None:
        fields["lyrics_synced"] = payload.lyrics_lrc

    success = await loop.run_in_executor(
        None, lambda: write_tags(target, fields, cover_bytes=cover_bytes)
    )

    if not success:
        raise HTTPException(status_code=500, detail="Failed to write tags to target audio file/folder.")

    if payload.rescan:
        async def _trigger_rescan():
            try:
                await loop.run_in_executor(None, lambda: services.navidrome.scan_path(target))
            except Exception as e:
                logger.warning(f"Background rescan failed: {e}")
        asyncio.create_task(_trigger_rescan())

    updated_tags = await loop.run_in_executor(None, lambda: read_tags(target))

    return {
        "ok": True,
        "message": f"Successfully updated tags for {target.name}",
        "path": str(target.relative_to(config.BASE_DOWNLOAD_DIR)),
        "updated_tags": updated_tags,
    }



# Mount API routes under both /api and /hub/api to support reverse proxy subpaths
app.include_router(api_router, prefix="/api")
app.include_router(api_router, prefix="/hub/api")

