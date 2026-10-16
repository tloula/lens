from __future__ import annotations

import httpx
import pytest
from conftest import jpeg, pixabay_hit
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.types import TextContent
from starlette.testclient import TestClient

from lens.mcp_server import (
    DownloadSigner,
    StaticBearerVerifier,
    build_app,
    build_auth,
    build_mcp,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def pixabay(mock_http, monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    monkeypatch.setenv("PIXABAY_API_KEY", "server-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pixabay.com" and request.url.path == "/api/":
            return httpx.Response(200, json={"hits": [pixabay_hit()], "totalHits": 1})
        return httpx.Response(200, content=jpeg(), headers={"content-type": "image/jpeg"})

    return mock_http(handler)


async def test_search_returns_compact_candidates(pixabay: list[httpx.Request]) -> None:
    async with Client(build_mcp(None)) as client:
        result = await client.call_tool("search_photos", {"query": "door", "limit": 3})
    photo = result.data["photos"][0]
    assert photo["id"] == "123"
    assert photo["credit_markdown"].startswith("Photo by [Sample Person]")
    assert photo["sizes"] == ["large", "medium", "small"]
    assert "downloads" not in photo


async def test_get_photo_includes_download_urls(pixabay: list[httpx.Request]) -> None:
    async with Client(build_mcp(None)) as client:
        result = await client.call_tool("get_photo", {"photo_id": "123"})
    assert result.data["downloads"]["large"] == "https://pixabay.com/get/large.jpg"


async def test_view_photo_returns_the_image(pixabay: list[httpx.Request]) -> None:
    async with Client(build_mcp(None)) as client:
        result = await client.call_tool("view_photo", {"photo_id": "123"})
    assert result.content[0].type == "image"
    assert result.content[0].mime_type == "image/jpeg"
    caption = result.content[1]
    assert isinstance(caption, TextContent)
    assert "Pixabay" in caption.text
    # The image comes from the CDN without the API key.
    assert "key" not in pixabay[-1].url.params


async def test_errors_are_tool_errors_without_secrets() -> None:
    async with Client(build_mcp(None)) as client:
        with pytest.raises(ToolError, match="PIXABAY_API_KEY"):
            await client.call_tool("search_photos", {"query": "door"})


def test_auth_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LENS_MCP_AUTH", "none")
    assert build_auth() is None
    monkeypatch.setenv("LENS_MCP_AUTH", "github")
    monkeypatch.delenv("LENS_ALLOWED_GITHUB_LOGINS", raising=False)
    with pytest.raises(RuntimeError, match="LENS_ALLOWED_GITHUB_LOGINS"):
        build_auth()
    with pytest.raises(ValueError, match="32 characters"):
        StaticBearerVerifier("short")


def test_token_auth_guards_mcp_but_not_healthz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LENS_MCP_AUTH", "token")
    monkeypatch.setenv("LENS_MCP_TOKEN", "t" * 40)
    with TestClient(build_app()) as http:
        assert http.get("/healthz").json()["ok"] is True
        assert http.post("/mcp", json={}).status_code == 401


async def test_download_photo_link_serves_the_image_through_lens(
    pixabay: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LENS_MCP_AUTH", "none")
    signer = DownloadSigner(b"s" * 32, base_url="https://lens.example.com")
    async with Client(build_mcp(None, signer=signer)) as client:
        result = await client.call_tool("download_photo", {"photo_id": "123"})
    link = result.data
    assert link["download_url"].startswith("https://lens.example.com/download/pixabay/123/large?")
    assert link["filename"] == "pixabay-123.jpg"
    assert link["credit_markdown"].startswith("Photo by [Sample Person]")

    path = link["download_url"].removeprefix("https://lens.example.com")
    with TestClient(build_app(signer=signer)) as http:
        response = http.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content == jpeg()
        assert http.get(path.replace("sig=", "sig=0")).status_code == 403
        assert http.get(path.replace("/large?", "/small?")).status_code == 403
    assert pixabay[-1].url.host == "pixabay.com"
    assert pixabay[-1].url.path == "/get/large.jpg"


async def test_download_photo_rejects_unknown_sizes(pixabay: list[httpx.Request]) -> None:
    async with Client(build_mcp(None)) as client:
        with pytest.raises(ToolError, match="Unknown size"):
            await client.call_tool("download_photo", {"photo_id": "123", "size": "huge"})


def test_expired_download_links_are_rejected() -> None:
    signer = DownloadSigner(b"s" * 32, ttl=-1)
    url, expires = signer.sign("pixabay", "123", "large")
    signature = url.rsplit("sig=", 1)[1]
    assert not signer.verify("pixabay", "123", "large", str(expires), signature)
