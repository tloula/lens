"""The provider contract and the shared HTTP call that keeps API keys out of errors."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, ClassVar, Protocol

import httpx

from lens.cache import ResponseCache
from lens.models import LensError, Photo, SearchResult


class Provider(Protocol):
    def __init__(
        self,
        api_key: str | None,
        *,
        cache: ResponseCache | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None: ...

    name: ClassVar[str]
    label: ClassVar[str]
    # HTTPS hosts that may serve this provider's image bytes.
    image_hosts: ClassVar[frozenset[str]]

    def search(
        self, query: str, *, page: int, limit: int, orientation: str | None
    ) -> SearchResult: ...

    def get(self, photo_id: str) -> Photo: ...


@contextmanager
def _without_http_telemetry() -> Iterator[None]:
    """Keep OpenTelemetry HTTP spans (which record URLs) off key-bearing requests."""
    try:
        from opentelemetry.instrumentation.utils import (  # pyright: ignore[reportMissingImports]
            suppress_http_instrumentation,
        )
    except ImportError:
        yield
        return
    with suppress_http_instrumentation():
        yield


def get_json(
    label: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """GET a provider JSON object, raising only key-free ``LensError`` messages."""
    # Errors are raised ``from None``: httpx exceptions can embed the key-bearing URL.
    try:
        with _without_http_telemetry(), httpx.Client(timeout=30, transport=transport) as client:
            response = client.get(url, params=params, headers=headers)
    except httpx.HTTPError:
        raise LensError(f"Could not reach the {label} API.") from None
    status = response.status_code
    if status == 429:
        raise LensError(f"{label} rate limit reached; retry later.", status=429)
    if status in (401, 403):
        raise LensError(f"{label} rejected the API key.")
    if status == 404:
        raise LensError(f"{label} photo not found.", status=404)
    if status == 400:
        raise LensError(f"{label} rejected the request; check the API key and parameters.")
    if response.is_error:
        raise LensError(f"{label} API returned HTTP {status}.")
    try:
        result = response.json()
    except ValueError:
        raise LensError(f"{label} returned an unreadable response.") from None
    if not isinstance(result, dict):
        raise LensError(f"{label} returned an unexpected response.")
    return result


def require_key(label: str, api_key: str | None) -> str:
    if not api_key:
        raise LensError(f"No {label} API key is configured.")
    return api_key


def positive_id(label: str, photo_id: str) -> str:
    if not photo_id.isdecimal() or int(photo_id) < 1:
        raise LensError(f"{label} photo IDs are positive integers.", status=422)
    return photo_id


def clean_name(name: object) -> str:
    """Strip characters that would break the Markdown credit link."""
    return str(name).replace("[", "").replace("]", "").strip()
