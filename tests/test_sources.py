from __future__ import annotations

from dataclasses import asdict

import httpx
import pytest
from conftest import pixabay_hit
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from lens.models import LensError
from lens.providers.pixabay import PixabayProvider
from lens.server import create_router
from lens.sources import DirectSource, RemoteSource


def test_direct_source_explains_missing_key() -> None:
    with pytest.raises(LensError, match=r"PIXABAY_API_KEY.*lens auth set pixabay"):
        DirectSource("pixabay")


def test_direct_source_reads_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIXABAY_API_KEY", "env-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == "env-key"
        return httpx.Response(200, json={"hits": [pixabay_hit()], "totalHits": 1})

    source = DirectSource("pixabay", transport=httpx.MockTransport(handler))
    assert source.search("door").photos[0].id == "123"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com",
        "https://user:pw@api.example.com",
        "https://api.example.com?x=1",
        "ftp://localhost",
        "https://",
    ],
)
def test_remote_source_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(LensError, match="Backend URL"):
        RemoteSource(url, "pixabay")


def test_remote_source_rejects_path_ids() -> None:
    source = RemoteSource("http://localhost:8000/photos", "pixabay")
    for bad in ("../search", "search", "a/b", ""):
        with pytest.raises(LensError, match="Photo IDs"):
            source.get(bad)


def test_remote_source_maps_error_envelopes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": {"message": "Pixabay rate limit reached."}})

    source = RemoteSource(
        "https://api.example.com/photos", "pixabay", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(LensError, match="HTTP 502: Pixabay rate limit"):
        source.get("1")


def test_remote_source_wraps_token_failures() -> None:
    def token() -> str:
        raise RuntimeError("secret detail")

    source = RemoteSource("https://api.example.com/photos", "pixabay", token=token)
    with pytest.raises(LensError, match="backend token") as error:
        source.get("1")
    assert "secret detail" not in str(error.value)


def test_remote_source_round_trips_through_server() -> None:
    provider_requests = []

    def provider_handler(request: httpx.Request) -> httpx.Response:
        provider_requests.append(request)
        return httpx.Response(200, json={"hits": [pixabay_hit()], "totalHits": 1})

    def require_token(authorization: str = Header()) -> None:
        if authorization != "Bearer good":
            raise HTTPException(status_code=401)

    app = FastAPI()
    router = create_router(lambda: {"pixabay": "server-key"}, dependencies=[Depends(require_token)])
    app.include_router(router, prefix="/api")

    original = PixabayProvider.__init__

    def init(self, api_key, *, cache=None, transport=None):
        original(self, api_key, cache=cache, transport=httpx.MockTransport(provider_handler))

    with pytest.MonkeyPatch.context() as patch, TestClient(app) as client:
        patch.setattr(PixabayProvider, "__init__", init)

        def forward(request: httpx.Request) -> httpx.Response:
            response = client.request(
                request.method,
                request.url.raw_path.decode(),
                headers=dict(request.headers),
            )
            return httpx.Response(response.status_code, content=response.content)

        source = RemoteSource(
            "http://localhost/api/photos",
            "pixabay",
            token=lambda: "good",
            headers={"X-Tenant-Id": "t"},
            transport=httpx.MockTransport(forward),
        )
        result = source.search("door", limit=1, orientation="landscape")
        assert asdict(result.photos[0]) == asdict(PixabayProvider._photo(pixabay_hit()))
        assert source.get("123").id == "123"
        assert provider_requests[0].url.params["key"] == "server-key"

        denied = RemoteSource(
            "http://localhost/api/photos",
            "pixabay",
            token=lambda: "bad",
            transport=httpx.MockTransport(forward),
        )
        with pytest.raises(LensError, match="rejected the credentials"):
            denied.get("123")
        assert (
            client.get("/api/photos/flickr/1", headers={"Authorization": "Bearer good"}).status_code
            == 404
        )


def test_server_reports_provider_errors_without_keys() -> None:
    app = FastAPI()
    app.include_router(create_router(dict))
    with TestClient(app) as client:
        response = client.get("/photos/pixabay/123")
    assert response.status_code == 424  # Not 502, which Cloudflare replaces.
    assert response.json()["detail"] == "No Pixabay API key is configured."


def test_remote_source_reports_redirects_without_following() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://evil.example/photos/x"})

    source = RemoteSource(
        "https://api.example.com/photos",
        "pixabay",
        token=lambda: "t",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LensError, match=r"redirected to https://evil.example"):
        source.get("1")
    assert len(requests) == 1


def test_remote_source_rejects_unsafe_photo_ids_from_backend() -> None:
    photo = asdict(PixabayProvider._photo(pixabay_hit()))
    source = RemoteSource(
        "https://api.example.com/photos",
        "pixabay",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={**photo, "id": "../../.ssh/key"})
        ),
    )
    with pytest.raises(LensError, match="Invalid photo metadata"):
        source.get("1")


def test_server_maps_error_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    original = PixabayProvider.__init__

    def init(self, api_key, *, cache=None, transport=None):
        original(
            self,
            api_key,
            cache=cache,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"hits": [], "totalHits": 0})
            ),
        )

    monkeypatch.setattr(PixabayProvider, "__init__", init)
    app = FastAPI()
    app.include_router(create_router(lambda: {"pixabay": "k"}))
    with TestClient(app) as client:
        assert client.get("/photos/pixabay/999").status_code == 404
        assert client.get("/photos/pixabay/abc").status_code == 422
        square = client.get(
            "/photos/pixabay/search", params={"query": "x", "orientation": "square"}
        )
        assert square.status_code == 422
