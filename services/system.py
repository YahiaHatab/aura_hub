"""System-level operations, storage metrics, binary tool checks, and library file management."""

import logging
import os
import platform
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import config
from utils.helpers import sanitize_filename

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
    """Returns sorted relative paths of all subdirectories containing MP3 or Opus audio files."""
    root_path = Path(base_dir or config.BASE_DOWNLOAD_DIR).resolve()
    folders: List[str] = []

    if not root_path.exists():
        return folders

    for current_dir, _, files in os.walk(str(root_path)):
        if any(f.lower().endswith((".mp3", ".opus")) for f in files):
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


def rehome_album_folder(
    current_folder: Union[str, Path],
    canonical_artist: str,
    base_dir: Optional[Union[str, Path]] = None,
) -> Path:
    """Safely relocates an album directory to match the Latin-Canonical artist folder.

    Example:
        Moves '~/Music/عمرو دياب/Saharna Ya Lail' -> '~/Music/Amr Diab/Saharna Ya Lail'
        Merges files if the target destination already exists, and cleans up empty parent directories.
    """
    root_path = Path(base_dir or config.BASE_DOWNLOAD_DIR).resolve()
    cur_path = Path(current_folder).resolve()

    # Guard: verify cur_path is within root_path
    try:
        rel = cur_path.relative_to(root_path)
    except ValueError:
        logger.warning(f"Re-home skipped: {cur_path} is outside base directory {root_path}")
        return cur_path

    if cur_path == root_path:
        return cur_path

    clean_artist = sanitize_filename(canonical_artist.strip())
    if not clean_artist or clean_artist.lower() in ("unknown artist", "various", "various artists"):
        return cur_path

    parts = rel.parts
    if len(parts) >= 2:
        current_artist_dir = parts[0]
        album_subpath = Path(*parts[1:])
        if current_artist_dir == clean_artist:
            # Already in canonical artist directory
            return cur_path
        dest_folder = (root_path / clean_artist / album_subpath).resolve()
    elif len(parts) == 1:
        album_name = parts[0]
        dest_folder = (root_path / clean_artist / album_name).resolve()
    else:
        return cur_path

    if dest_folder == cur_path:
        return cur_path

    # Security check on destination path
    try:
        dest_folder.relative_to(root_path)
    except ValueError:
        logger.warning(f"Re-home security violation: {dest_folder} outside {root_path}")
        return cur_path

    try:
        if dest_folder.exists():
            logger.info(f"Target folder {dest_folder} exists; merging files from {cur_path}")
            for item in list(cur_path.iterdir()):
                target_item = dest_folder / item.name
                if target_item.exists():
                    if item.is_dir():
                        shutil.copytree(str(item), str(target_item), dirs_exist_ok=True)
                        shutil.rmtree(str(item))
                    else:
                        target_item.unlink()
                        shutil.move(str(item), str(target_item))
                else:
                    shutil.move(str(item), str(target_item))
            try:
                cur_path.rmdir()
            except Exception as e:
                logger.warning(f"Could not remove source folder after merge: {e}")
        else:
            dest_folder.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(cur_path), str(dest_folder))
            logger.info(f"Relocated album folder: {cur_path} -> {dest_folder}")

        # Clean up empty parent artist directory
        old_parent = cur_path.parent
        while old_parent != root_path and old_parent.is_dir():
            try:
                if not any(old_parent.iterdir()):
                    old_parent.rmdir()
                    logger.info(f"Cleaned empty parent artist directory: {old_parent}")
                    old_parent = old_parent.parent
                else:
                    break
            except Exception:
                break

        return dest_folder
    except Exception as e:
        logger.exception(f"Failed to re-home album folder {cur_path} to {dest_folder}: {e}")
        return cur_path


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
