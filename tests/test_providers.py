from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
from conftest import pixabay_hit

from lens.cache import ResponseCache
from lens.models import LensError
from lens.providers.pexels import PexelsProvider
from lens.providers.pixabay import PixabayProvider


def pixabay(handler, cache_dir: Path) -> PixabayProvider:
    return PixabayProvider(
        "private-key", cache=ResponseCache(cache_dir), transport=httpx.MockTransport(handler)
    )


def test_pixabay_maps_parameters_and_caches_without_the_key(tmp_path: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.params["key"] == "private-key"
        assert request.url.params["orientation"] == "horizontal"
        assert request.url.params["safesearch"] == "true"
        assert request.url.params["per_page"] == "3"
        return httpx.Response(200, json={"hits": [pixabay_hit()] * 3, "totalHits": 40})

    provider = pixabay(handler, tmp_path)
    first = provider.search("door", page=1, limit=1, orientation="landscape")
    assert provider.search("door", page=1, limit=1, orientation="landscape") == first
    assert len(calls) == 1
    assert len(first.photos) == 1
    assert first.total_results == 40
    photo = first.photos[0]
    assert photo.provider == "pixabay"
    assert photo.downloads["large"] == "https://pixabay.com/get/large.jpg"
    assert photo.credit_markdown == (
        "Photo by [Sample Person](https://pixabay.com/photos/door-123/) on "
        "[Pixabay](https://pixabay.com)."
    )
    cached = "".join(path.read_text() for path in tmp_path.glob("*.json"))
    assert cached and "private-key" not in cached


def test_pixabay_cache_expires(tmp_path: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"hits": [pixabay_hit()], "totalHits": 1})

    provider = PixabayProvider(
        "k", cache=ResponseCache(tmp_path, ttl=0), transport=httpx.MockTransport(handler)
    )
    provider.get("123")
    provider.get("123")
    assert len(calls) == 2


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_pixabay_errors_never_include_the_key(tmp_path: Path, status: int) -> None:
    provider = pixabay(lambda request: httpx.Response(status), tmp_path)
    with pytest.raises(LensError) as error:
        provider.get("123")
    assert "private-key" not in str(error.value)
    assert error.value.__cause__ is None


def test_pixabay_network_error_hides_url(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed {request.url}")

    with pytest.raises(LensError) as error:
        pixabay(handler, tmp_path).get("123")
    assert "private-key" not in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_pixabay_http_logs_redact_key(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    provider = pixabay(
        lambda request: httpx.Response(200, json={"hits": [pixabay_hit()], "totalHits": 1}),
        tmp_path,
    )
    with caplog.at_level(logging.INFO, logger="httpx"):
        provider.get("123")
    assert "private-key" not in caplog.text
    assert "[redacted]" in caplog.text


def test_pixabay_validation(tmp_path: Path) -> None:
    provider = pixabay(lambda request: httpx.Response(500), tmp_path)
    with pytest.raises(LensError, match="positive integers"):
        provider.get("../x")
    with pytest.raises(LensError, match="landscape or portrait"):
        provider.search("door", page=1, limit=3, orientation="square")
    with pytest.raises(LensError, match="No Pixabay API key"):
        PixabayProvider(None, cache=ResponseCache(tmp_path)).get("1")
    with pytest.raises(LensError, match="page URL"):
        PixabayProvider._photo(pixabay_hit(pageURL="https://evil.example/door"))
    empty = pixabay(
        lambda request: httpx.Response(200, json={"hits": [], "totalHits": 0}), tmp_path
    )
    with pytest.raises(LensError, match="not found"):
        empty.get("999")


def test_pexels_maps_photos_and_sends_key_in_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "pexels-key"
        assert "pexels-key" not in str(request.url)
        return httpx.Response(
            200,
            json={
                "id": 7,
                "url": "https://www.pexels.com/photo/door-7/",
                "width": 4000,
                "height": 3000,
                "alt": "A door",
                "photographer": "Pat",
                "src": {
                    "original": "https://images.pexels.com/o.jpg",
                    "large2x": "https://images.pexels.com/l2.jpg",
                    "medium": "https://images.pexels.com/m.jpg",
                },
            },
        )

    photo = PexelsProvider("pexels-key", transport=httpx.MockTransport(handler)).get("7")
    assert photo.provider == "pexels"
    assert photo.downloads["large"] == "https://images.pexels.com/l2.jpg"
    assert photo.preview_url == "https://images.pexels.com/m.jpg"
    assert "[Pexels](https://www.pexels.com)" in photo.credit_markdown
