from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from conftest import jpeg, pixabay_hit

from lens import download
from lens.download import fetch_image, load_download, save_photo, validate_image
from lens.models import LensError, Photo
from lens.providers.pixabay import PixabayProvider


def photo() -> Photo:
    return PixabayProvider._photo(pixabay_hit())


def test_save_and_load_round_trip(mock_http, tmp_path: Path) -> None:
    requests = mock_http(lambda request: httpx.Response(200, content=jpeg()))
    result = save_photo(photo(), tmp_path)
    assert result.file == tmp_path / "pixabay-123.jpg"
    assert result.summary()["credit_markdown"] == photo().credit_markdown
    assert "authorization" not in requests[0].headers
    record = json.loads(result.metadata.read_text())
    assert record["photo"]["source_url"] == photo().source_url
    loaded, content, mime = load_download(result.file)
    assert loaded == photo() and mime == "image/jpeg" and content == jpeg()

    with pytest.raises(LensError, match="already exists"):
        save_photo(photo(), result.file)
    result.file.write_bytes(b"changed")
    with pytest.raises(LensError, match="differs"):
        load_download(result.file)


def test_load_requires_sidecar(tmp_path: Path) -> None:
    file = tmp_path / "loose.jpg"
    file.write_bytes(jpeg())
    with pytest.raises(LensError, match=r"loose\.jpg\.photo\.json"):
        load_download(file)


def test_fetch_rejects_unexpected_hosts_and_sizes(mock_http) -> None:
    requests = mock_http(lambda request: httpx.Response(200, content=jpeg()))
    hostile = replace(photo(), downloads={"large": "https://example.com/x.jpg"})
    with pytest.raises(LensError, match="unexpected image URL"):
        fetch_image(hostile)
    with pytest.raises(LensError, match="available: large"):
        fetch_image(photo(), "huge")
    assert not requests


def test_fetch_enforces_size_and_format(mock_http, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_http(lambda request: httpx.Response(200, content=b"<html>not an image</html>"))
    with pytest.raises(LensError, match="not a valid"):
        fetch_image(photo())
    monkeypatch.setattr(download, "MAX_IMAGE_BYTES", 10)
    mock_http(lambda request: httpx.Response(200, content=jpeg()))
    with pytest.raises(LensError, match="25 MB"):
        fetch_image(photo())


def test_validate_image_rejects_empty() -> None:
    with pytest.raises(LensError):
        validate_image(b"")
