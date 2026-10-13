"""Pixabay: free stock photos; API responses must be cached for 24 hours."""

from __future__ import annotations

import logging
import re
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from lens.cache import ResponseCache
from lens.models import LensError, Photo, SearchResult
from lens.providers.base import clean_name, get_json, positive_id, require_key

API_URL = "https://pixabay.com/api/"


class _RedactApiKey(logging.Filter):
    """Pixabay authenticates in the query string, which httpx logs at INFO."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "pixabay.com/api/" in message:
            record.msg = re.sub(r"(\bkey=)[^&\s\"]+", r"\1[redacted]", message)
            record.args = ()
        return True


logging.getLogger("httpx").addFilter(_RedactApiKey())


class PixabayProvider:
    name: ClassVar[str] = "pixabay"
    label: ClassVar[str] = "Pixabay"
    image_hosts: ClassVar[frozenset[str]] = frozenset({"pixabay.com", "cdn.pixabay.com"})

    def __init__(
        self,
        api_key: str | None,
        *,
        cache: ResponseCache | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.cache = cache or ResponseCache()
        self.transport = transport

    def _api(self, params: dict[str, Any]) -> dict[str, Any]:
        key = require_key(self.label, self.api_key)
        request = {"key": key, **params}
        if (cached := self.cache.get(request)) is not None:
            return cached
        result = get_json(self.label, API_URL, params=request, transport=self.transport)
        self.cache.put(request, result)
        return result

    def search(self, query: str, *, page: int, limit: int, orientation: str | None) -> SearchResult:
        if len(query) > 100:
            raise LensError("Pixabay queries must be at most 100 characters.")
        if orientation == "square":
            raise LensError("Pixabay only filters landscape or portrait orientation.")
        params: dict[str, Any] = {
            "q": query,
            "page": page,
            "per_page": max(3, limit),  # Pixabay's minimum page size.
            "image_type": "photo",
            "safesearch": "true",
        }
        if orientation:
            params["orientation"] = {"landscape": "horizontal", "portrait": "vertical"}[orientation]
        data = self._api(params)
        try:
            photos = [self._photo(hit) for hit in data["hits"][:limit]]
            return SearchResult(photos, page, int(data["totalHits"]))
        except (KeyError, TypeError, ValueError):
            raise LensError("Invalid Pixabay search response.") from None

    def get(self, photo_id: str) -> Photo:
        data = self._api({"id": positive_id(self.label, photo_id), "image_type": "photo"})
        try:
            hits = data["hits"]
            if not hits:
                raise LensError("Pixabay photo not found.")
            return self._photo(hits[0])
        except (KeyError, TypeError, ValueError):
            raise LensError("Invalid Pixabay photo response.") from None

    @staticmethod
    def _photo(data: dict[str, Any]) -> Photo:
        url = str(data["pageURL"])
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "pixabay.com":
            raise LensError("Invalid Pixabay photo page URL.")
        user = clean_name(data["user"])
        downloads = {
            "large": data["largeImageURL"],
            "medium": data["webformatURL"],
            "small": data["previewURL"],
        }
        if data.get("imageURL"):
            downloads["original"] = data["imageURL"]
        return Photo(
            provider="pixabay",
            id=str(data["id"]),
            source_url=url,
            preview_url=data["webformatURL"],
            width=int(data["imageWidth"]),
            height=int(data["imageHeight"]),
            alt=str(data.get("tags") or ""),
            photographer=user,
            credit_markdown=f"Photo by [{user}]({url}) on [Pixabay](https://pixabay.com).",
            downloads=downloads,
        )
