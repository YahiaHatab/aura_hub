"""In-memory task manager for tracking background audio downloads and ingestion pipelines."""

import logging
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

import urllib.parse
from pathlib import Path

import config
from services.downloader import executor, run_pipeline
from services.navidrome import navidrome_client

logger = logging.getLogger(__name__)

# In-memory dictionary: task_id -> task_dict
_tasks_lock = threading.Lock()
_tasks: Dict[str, Dict[str, Any]] = {}


def create_task(url: str, started_by: str = "Admin") -> Dict[str, Any]:
    """Registers a new active download task in memory."""
    task_id = secrets.token_hex(4)
    clean_url = url.strip()
    task_data = {
        "id": task_id,
        "url": clean_url,
        "source_url": clean_url,
        "title": None,
        "artist": None,
        "cover_url": None,
        "stage": "Queued",
        "progress": 5,
        "status": "active",  # "active", "completed", "failed"
        "message": "Task queued for download...",
        "error": None,
        "started_by": started_by,
        "created_at": time.time(),
        "completed_at": None,
    }
    with _tasks_lock:
        _tasks[task_id] = task_data
    return task_data


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves a specific task by its ID."""
    with _tasks_lock:
        return _tasks.get(task_id)


def get_all_tasks(limit: int = 20) -> List[Dict[str, Any]]:
    """Returns a list of tasks sorted by creation time descending."""
    with _tasks_lock:
        task_list = list(_tasks.values())
    task_list.sort(key=lambda t: t.get("created_at", 0), reverse=True)
    return task_list[:limit]


def _run_download_task_worker(task_id: str):
    """Worker function executed inside ThreadPoolExecutor to run the pipeline and update task state."""
    with _tasks_lock:
        task = _tasks.get(task_id)
    if not task:
        return

    url = task["url"]

    def update_task_progress(msg: str):
        lower_msg = msg.lower()
        stage = "Downloading"
        prog = 30

        if "1/4" in lower_msg or "extracting" in lower_msg or "streaming" in lower_msg:
            stage = "Downloading"
            prog = 30
        elif "2/4" in lower_msg or "3/4" in lower_msg or "tagging" in lower_msg:
            stage = "Tagging"
            prog = 60
        elif "4/4" in lower_msg or "lyrics" in lower_msg:
            stage = "Lyrics"
            prog = 80

        with _tasks_lock:
            if task_id in _tasks:
                _tasks[task_id]["stage"] = stage
                _tasks[task_id]["progress"] = prog
                _tasks[task_id]["message"] = msg

    try:
        with _tasks_lock:
            _tasks[task_id]["stage"] = "Downloading"
            _tasks[task_id]["progress"] = 20
            _tasks[task_id]["message"] = "Initializing download..."

        target_folder, meta = run_pipeline(url, status_updater=update_task_progress)

        with _tasks_lock:
            if task_id in _tasks:
                _tasks[task_id]["stage"] = "Library Scan"
                _tasks[task_id]["progress"] = 92
                _tasks[task_id]["message"] = "Triggering Navidrome library scan..."

        if navidrome_client.is_configured():
            navidrome_client.start_scan()

        album_title = meta.get("album", "Album")
        artist = meta.get("artist", "Artist")

        cover_url = None
        try:
            rel_folder = Path(target_folder).resolve().relative_to(config.BASE_DOWNLOAD_DIR.resolve())
            clean_rel = str(rel_folder).replace("\\", "/")
            cover_url = f"api/cover?path={urllib.parse.quote(clean_rel)}"
        except Exception:
            pass

        with _tasks_lock:
            if task_id in _tasks:
                _tasks[task_id]["title"] = album_title
                _tasks[task_id]["artist"] = artist
                _tasks[task_id]["cover_url"] = cover_url
                _tasks[task_id]["stage"] = "Complete"
                _tasks[task_id]["progress"] = 100
                _tasks[task_id]["status"] = "completed"
                _tasks[task_id]["message"] = f"Finished: {artist} — {album_title}"
                _tasks[task_id]["completed_at"] = time.time()

    except Exception as e:
        logger.exception(f"Download task {task_id} failed: {e}")
        with _tasks_lock:
            if task_id in _tasks:
                _tasks[task_id]["stage"] = "Failed"
                _tasks[task_id]["progress"] = 100
                _tasks[task_id]["status"] = "failed"
                _tasks[task_id]["error"] = str(e)
                _tasks[task_id]["message"] = f"Failed: {e}"
                _tasks[task_id]["completed_at"] = time.time()


def start_download_task(url: str, started_by: str = "Admin") -> Dict[str, Any]:
    """Creates a download task and launches it asynchronously in the thread pool executor."""
    task = create_task(url, started_by=started_by)
    executor.submit(_run_download_task_worker, task["id"])
    return task
