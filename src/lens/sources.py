"""Where photo metadata comes from: providers directly, or a lens-compatible backend."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from lens.cache import ResponseCache
from lens.models import LensError, Photo, SearchResult
from lens.providers import create_provider, provider_class
from lens.secrets import resolve_key

SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


class Source(Protocol):
    provider: str

    def search(
        self, query: str, *, page: int = 1, limit: int = 12, orientation: str | None = None
    ) -> SearchResult: ...

    def get(self, photo_id: str) -> Photo: ...


class DirectSource:
    """Call the provider API with a local key (argument, environment, or OS keyring)."""

    def __init__(
        self,
        provider: str,
        api_key: str | None = None,
        *,
        cache: ResponseCache | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.provider = provider
        self._client = create_provider(
            provider, api_key or resolve_key(provider), cache=cache, transport=transport
        )

    def search(
        self, query: str, *, page: int = 1, limit: int = 12, orientation: str | None = None
    ) -> SearchResult:
        return self._client.search(query, page=page, limit=limit, orientation=orientation)

    def get(self, photo_id: str) -> Photo:
        return self._client.get(photo_id)


class RemoteSource:
    """Call a backend serving ``lens.server`` routes; provider keys stay on the server."""

    def __init__(
        self,
        base_url: str,
        provider: str,
        *,
        token: Callable[[], str] | None = None,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        provider_class(provider)
        url = urlsplit(base_url)
        secure = url.scheme == "https" or (url.scheme == "http" and url.hostname in LOOPBACK)
        if (
            not secure
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise LensError(
                "Backend URL must be https:// (or http:// on localhost) without credentials, "
                "query, or fragment."
            )
        self.base_url = base_url.rstrip("/")
        self.provider = provider
        self.token = token
        self.headers = dict(headers or {})
        self.transport = transport

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = dict(self.headers)
        if self.token is not None:
            try:
                headers["Authorization"] = f"Bearer {self.token()}"
            except LensError:
                raise
            except Exception:
                raise LensError("Could not obtain a backend token.") from None
        url = f"{self.base_url}/{self.provider}/{path}"
        try:
            with httpx.Client(timeout=60, transport=self.transport) as client:
                response = client.get(url, params=params, headers=headers)
        except httpx.HTTPError:
            raise LensError("Could not reach the photo backend.") from None
        if response.is_error:
            raise LensError(_error_message(response))
        try:
            result = response.json()
        except ValueError:
            raise LensError("Photo backend returned an unreadable response.") from None
        if not isinstance(result, dict):
            raise LensError("Photo backend returned an unexpected response.")
        return result

    def search(
        self, query: str, *, page: int = 1, limit: int = 12, orientation: str | None = None
    ) -> SearchResult:
        params: dict[str, Any] = {"query": query, "page": page, "limit": limit}
        if orientation:
            params["orientation"] = orientation
        return SearchResult.from_dict(self._get("search", params))

    def get(self, photo_id: str) -> Photo:
        if not SAFE_ID.fullmatch(photo_id) or photo_id == "search":
            raise LensError("Photo IDs may only contain letters, digits, '-' and '_'.")
        return Photo.from_dict(self._get(photo_id))


def _error_message(response: httpx.Response) -> str:
    status = response.status_code
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            error = body.get("error")
            detail = (error.get("message") if isinstance(error, dict) else None) or body.get(
                "detail"
            )
            detail = detail if isinstance(detail, str) else ""
    except ValueError:
        pass
    if status in (401, 403):
        return f"Photo backend rejected the credentials (HTTP {status}). {detail}".strip()
    if status == 404 and not detail:
        return "Photo backend route or photo not found (HTTP 404)."
    return f"Photo backend returned HTTP {status}: {detail or 'request failed'}"
