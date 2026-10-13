"""Per-user config and cache locations, plus owner-only file writes."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path


def config_path() -> Path:
    if override := os.environ.get("LENS_CONFIG"):
        return Path(override)
    if sys.platform == "win32" and (appdata := os.environ.get("APPDATA")):
        return Path(appdata) / "lens" / "config.toml"
    root = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(root) / "lens" / "config.toml"


def cache_dir() -> Path:
    if override := os.environ.get("LENS_CACHE_DIR"):
        return Path(override)
    if sys.platform == "win32" and (local := os.environ.get("LOCALAPPDATA")):
        return Path(local) / "lens" / "cache"
    root = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(root) / "lens"


def private_directory(path: Path) -> bool:
    """Create ``path`` owner-only; return whether it is safe to read and write there."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "posix":
        return True  # Per-user profile directories are already private on Windows.
    stat = path.stat()
    return stat.st_uid == os.getuid() and not stat.st_mode & 0o022


def write_private(path: Path, text: str) -> None:
    """Atomically replace ``path`` with an owner-only file."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
