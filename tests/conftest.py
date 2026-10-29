from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from lens import secrets

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No real keys, keyring, config, or cache leak into tests."""
    for name in ("PIXABAY", "PEXELS"):
        monkeypatch.delenv(f"{name}_API_KEY", raising=False)
        monkeypatch.delenv(f"LENS_{name}_API_KEY", raising=False)
    for name in (
        "LENS_BACKEND",
        "LENS_PROVIDER",
        "LENS_TOKEN_COMMAND",
        "LENS_BACKEND_TOKEN",
        "BUCKET_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LENS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("LENS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(secrets, "_keyring_get", lambda provider: None)


@pytest.fixture
def mock_http(monkeypatch: pytest.MonkeyPatch) -> Callable[[Handler], list[httpx.Request]]:
    """Route every ``httpx.Client`` through ``handler``; returns the recorded requests."""

    def install(handler: Handler) -> list[httpx.Request]:
        requests: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return handler(request)

        factory = httpx.Client

        def client(**kwargs: Any) -> httpx.Client:
            kwargs["transport"] = httpx.MockTransport(record)
            return factory(**kwargs)

        monkeypatch.setattr(httpx, "Client", client)
        return requests

    return install


def jpeg() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (8, 6), "green").save(stream, format="JPEG")
    return stream.getvalue()


def pixabay_hit(**overrides: Any) -> dict[str, Any]:
    return {
        "id": 123,
        "pageURL": "https://pixabay.com/photos/door-123/",
        "webformatURL": "https://cdn.pixabay.com/medium.jpg",
        "largeImageURL": "https://pixabay.com/get/large.jpg",
        "previewURL": "https://cdn.pixabay.com/small.jpg",
        "imageWidth": 1280,
        "imageHeight": 800,
        "user": "Sample [Person]",
        "tags": "door, wood",
        **overrides,
    }
