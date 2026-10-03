"""System-level operations, storage metrics, binary tool checks, and library file management."""

import logging
import os
import platform
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import config

logger = logging.getLogger(__name__)


def check_tool_availability() -> Dict[str, bool]:
    """Inspects PATH and environment for required external CLI binaries."""
    tools = {
        "yt-dlp": bool(shutil.which("yt-dlp")),
        "spotdl": bool(
            shutil.which("spotdl")
            or (Path.home() / ".local" / "bin" / "spotdl").is_file()
        ),
        "fpcalc": bool(shutil.which("fpcalc")),
    }
    return tools


def get_disk_metrics(base_dir: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Calculates disk space usage and library statistics (MP3 and LRC count)."""
    target_dir = Path(base_dir or config.BASE_DOWNLOAD_DIR).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    total, used, free = shutil.disk_usage(str(target_dir))
    total_gb = total / (1024**3)
    used_gb = used / (1024**3)
    free_gb = free / (1024**3)
    pct = (used / total) * 100 if total > 0 else 0.0

    mp3_count = 0
    lrc_count = 0

    if target_dir.exists():
        for root, _, files in os.walk(str(target_dir)):
            for f in files:
                f_lower = f.lower()
                if f_lower.endswith(".mp3"):
                    mp3_count += 1
                elif f_lower.endswith(".lrc"):
                    lrc_count += 1

    return {
        "base_dir": str(target_dir),
        "total_gb": total_gb,
        "used_gb": used_gb,
        "free_gb": free_gb,
        "pct_used": pct,
        "mp3_count": mp3_count,
        "lrc_count": lrc_count,
    }


def get_album_folders(base_dir: Optional[Union[str, Path]] = None) -> List[str]:
    """Returns sorted relative paths of all subdirectories containing MP3 audio files."""
    root_path = Path(base_dir or config.BASE_DOWNLOAD_DIR).resolve()
    folders: List[str] = []

    if not root_path.exists():
        return folders

    for current_dir, _, files in os.walk(str(root_path)):
        if any(f.lower().endswith(".mp3") for f in files):
            rel_path = os.path.relpath(current_dir, str(root_path))
            if rel_path != ".":
                # Normalize slashes to forward slashes for uniform cross-platform handling
                folders.append(rel_path.replace("\\", "/"))

    folders.sort(key=str.lower)
    return folders


def delete_album_folder(
    rel_path: str, base_dir: Optional[Union[str, Path]] = None
) -> Tuple[bool, str]:
    """Safely removes an album directory and cleans empty parent folders.

    Guards against path traversal attacks by validating resolved paths.
    """
    root_path = Path(base_dir or config.BASE_DOWNLOAD_DIR).resolve()
    target_path = (root_path / rel_path).resolve()

    # Security check: verify that target path resides strictly inside root_path
    try:
        target_path.relative_to(root_path)
    except ValueError:
        return False, "Security violation: target directory is outside the base music folder."

    if not target_path.exists():
        return False, "Target folder does not exist."

    if target_path == root_path:
        return False, "Cannot delete the root music directory."

    try:
        shutil.rmtree(target_path)
        logger.info(f"Deleted album folder: {target_path}")

        # Clean empty parent artist folder if applicable
        parent_dir = target_path.parent
        if parent_dir != root_path and parent_dir.is_dir() and not any(parent_dir.iterdir()):
            parent_dir.rmdir()
            logger.info(f"Cleaned empty parent artist directory: {parent_dir}")

        return True, f"Successfully removed `{rel_path}` and freed disk space."

    except Exception as e:
        logger.exception(f"Failed to delete folder {target_path}")
        return False, f"Failed to delete directory: {e}"


def get_system_diagnostic_summary() -> str:
    """Produces an OS and tool availability summary."""
    tools = check_tool_availability()
    os_name = f"{platform.system()} {platform.release()}"

    lines = [
        f"🖥️ *Platform:* `{os_name}`",
        f"📦 *Python:* `{platform.python_version()}`",
        f"🛠️ *yt-dlp:* {'✅ Available' if tools['yt-dlp'] else '⚠️ Not Found'}",
        f"🛠️ *spotdl:* {'✅ Available' if tools['spotdl'] else '⚠️ Not Found'}",
        f"🛠️ *fpcalc:* {'✅ Available' if tools['fpcalc'] else '⚠️ Not Found (AcoustID disabled)'}",
    ]
    return "\n".join(lines)
