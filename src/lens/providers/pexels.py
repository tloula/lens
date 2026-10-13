"""Pexels: free stock photos authenticated with an Authorization header."""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from lens.cache import ResponseCache
from lens.models import LensError, Photo, SearchResult
from lens.providers.base import clean_name, get_json, positive_id, require_key

API_URL = "https://api.pexels.com/v1/"


class PexelsProvider:
    name: ClassVar[str] = "pexels"
    label: ClassVar[str] = "Pexels"
    image_hosts: ClassVar[frozenset[str]] = frozenset({"images.pexels.com"})

    def __init__(
        self,
        api_key: str | None,
        *,
        cache: ResponseCache | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key  # Pexels has no caching requirement, so ``cache`` is unused.
        self.transport = transport

    def _api(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": require_key(self.label, self.api_key)}
        return get_json(
            self.label, API_URL + path, params=params, headers=headers, transport=self.transport
        )

    def search(self, query: str, *, page: int, limit: int, orientation: str | None) -> SearchResult:
        params: dict[str, Any] = {"query": query, "page": page, "per_page": limit}
        if orientation:
            params["orientation"] = orientation
        data = self._api("search", params)
        try:
            photos = [self._photo(item) for item in data["photos"]]
            return SearchResult(photos, page, int(data["total_results"]))
        except (KeyError, TypeError, ValueError):
            raise LensError("Invalid Pexels search response.") from None

    def get(self, photo_id: str) -> Photo:
        data = self._api(f"photos/{positive_id(self.label, photo_id)}")
        try:
            return self._photo(data)
        except (KeyError, TypeError, ValueError):
            raise LensError("Invalid Pexels photo response.") from None

    @staticmethod
    def _photo(data: dict[str, Any]) -> Photo:
        url = str(data["url"])
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "www.pexels.com":
            raise LensError("Invalid Pexels photo page URL.")
        sizes = {str(name): str(link) for name, link in data["src"].items()}
        photographer = clean_name(data["photographer"])
        return Photo(
            provider="pexels",
            id=str(data["id"]),
            source_url=url,
            preview_url=sizes.get("medium", sizes["large2x"]),
            width=int(data["width"]),
            height=int(data["height"]),
            alt=str(data.get("alt") or ""),
            photographer=photographer,
            credit_markdown=(
                f"Photo by [{photographer}]({url}) on [Pexels](https://www.pexels.com)."
            ),
            downloads={**sizes, "large": sizes["large2x"]},
        )
