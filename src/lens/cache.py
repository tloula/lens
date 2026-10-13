"""Private on-disk cache for provider API responses (Pixabay requires 24 hours)."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from lens.paths import cache_dir, private_directory, write_private

DAY = 24 * 60 * 60
logger = logging.getLogger(__name__)


class ResponseCache:
    def __init__(self, directory: Path | None = None, ttl: float = DAY) -> None:
        self._directory = directory  # Resolved lazily so construction never touches disk.
        self.ttl = ttl
        self._warned = False

    def _path(self, request: dict[str, Any]) -> Path:
        directory = self._directory or cache_dir()
        if not private_directory(directory):
            raise PermissionError(f"{directory} is not private to this user")
        # The request may include an API key; only its hash is ever written to disk.
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        return directory / f"{digest}.json"

    def _disabled(self, exc: Exception) -> None:
        if not self._warned:
            self._warned = True
            logger.warning("lens response cache unavailable (set LENS_CACHE_DIR): %s", exc)

    def get(self, request: dict[str, Any]) -> dict[str, Any] | None:
        try:
            path = self._path(request)
        except (OSError, RuntimeError) as exc:  # RuntimeError: no resolvable home directory.
            self._disabled(exc)
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(entry["saved_at"]) < self.ttl:
                response = entry["response"]
                return response if isinstance(response, dict) else None
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Missing, expired, or corrupt entries are cache misses.
        return None

    def put(self, request: dict[str, Any], response: dict[str, Any]) -> None:
        try:
            entry = json.dumps({"saved_at": time.time(), "response": response})
            write_private(self._path(request), entry)
        except (OSError, RuntimeError) as exc:
            self._disabled(exc)  # Caching is best effort; the response is still returned.
