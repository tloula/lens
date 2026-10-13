"""Private on-disk cache for provider API responses (Pixabay requires 24 hours)."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from lens.paths import cache_dir, private_directory, write_private

DAY = 24 * 60 * 60


class ResponseCache:
    def __init__(self, directory: Path | None = None, ttl: float = DAY) -> None:
        self.directory = directory or cache_dir()
        self.ttl = ttl

    def _path(self, request: dict[str, Any]) -> Path:
        # The request may include an API key; only its hash is ever written to disk.
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        return self.directory / f"{digest}.json"

    def get(self, request: dict[str, Any]) -> dict[str, Any] | None:
        try:
            if not private_directory(self.directory):
                return None
            entry = json.loads(self._path(request).read_text(encoding="utf-8"))
            if time.time() - float(entry["saved_at"]) < self.ttl:
                response = entry["response"]
                return response if isinstance(response, dict) else None
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def put(self, request: dict[str, Any], response: dict[str, Any]) -> None:
        try:
            if private_directory(self.directory):
                entry = {"saved_at": time.time(), "response": response}
                write_private(self._path(request), json.dumps(entry))
        except OSError:
            pass  # Caching is best effort; the response is still returned.
