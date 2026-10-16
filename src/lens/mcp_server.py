"""Remote MCP server (``pip install lens[mcp]``): lens search, preview, and metadata as tools.

Run ``lens-mcp`` and connect a client to ``https://<host>/mcp``. Provider keys stay on the
server. Auth follows ``LENS_MCP_AUTH``: ``github`` (OAuth, for claude.ai connectors),
``token`` (a static bearer token, for Claude Code or scripts), or ``none`` (local only).
"""

# No ``from __future__ import annotations``: FastMCP reads the Annotated tool signatures.

import hmac
import logging
import os
from collections.abc import Callable
from dataclasses import asdict
from typing import Annotated, Any, Literal, TypeVar

import anyio.to_thread
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, AuthProvider, TokenVerifier
from fastmcp.utilities.types import Image
from pydantic import Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from lens import __version__
from lens.cache import ResponseCache
from lens.config import load_config
from lens.download import fetch_image
from lens.models import LensError, Photo
from lens.providers import PROVIDERS
from lens.sources import DirectSource

logger = logging.getLogger(__name__)

T = TypeVar("T")

READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}
GITHUB_TOKEN_CACHE_SECONDS = 300

INSTRUCTIONS = """lens finds free stock photos (Pixabay by default, Pexels too) with attribution
worked out. Workflow: `search_photos` for candidates, `view_photo` to look at the most promising
ones, then `get_photo` for download URLs. Download the chosen size to your own storage right
away (provider URLs can expire within 24 hours) instead of hotlinking it, and put the photo's
`credit_markdown` next to the image or in the page credits. Pick images whose subject is
obvious without explanation."""

ProviderName = Annotated[
    str | None,
    Field(description=f"One of {sorted(PROVIDERS)}. Defaults to the server's provider (pixabay)."),
]
PhotoId = Annotated[str, Field(description="A photo `id` from search_photos.")]


class AllowlistVerifier(TokenVerifier):
    """Wraps an upstream verifier and only admits the configured GitHub logins."""

    def __init__(self, inner: TokenVerifier, logins: set[str]) -> None:
        super().__init__(required_scopes=inner.required_scopes)
        self._inner = inner
        self._logins = {login.lower() for login in logins}

    async def verify_token(self, token: str) -> AccessToken | None:
        result = await self._inner.verify_token(token)
        if result is None:
            return None
        login = str(result.claims.get("login") or "").lower()
        if login not in self._logins:
            logger.warning("mcp auth: rejected GitHub login %r", login)
            return None
        return result


class StaticBearerVerifier(TokenVerifier):
    """Single shared bearer token (Claude Code / scripts). Constant-time compare."""

    def __init__(self, token: str) -> None:
        super().__init__()
        if len(token) < 32:
            raise ValueError("LENS_MCP_TOKEN must be at least 32 characters")
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self._token.encode()):
            return AccessToken(token=token, client_id="lens-static", scopes=[], claims={})
        return None


def build_auth() -> AuthProvider | None:
    mode = os.environ.get("LENS_MCP_AUTH", "github").lower()
    if mode == "none":
        logger.warning("MCP auth DISABLED (LENS_MCP_AUTH=none); only use this locally")
        return None
    if mode == "token":
        return StaticBearerVerifier(os.environ["LENS_MCP_TOKEN"])
    if mode == "github":
        from fastmcp.server.auth.providers.github import GitHubProvider

        logins = {
            login.strip()
            for login in os.environ.get("LENS_ALLOWED_GITHUB_LOGINS", "").split(",")
            if login.strip()
        }
        if not logins:
            raise RuntimeError("LENS_ALLOWED_GITHUB_LOGINS is required for github auth")
        provider = GitHubProvider(
            client_id=os.environ["GITHUB_CLIENT_ID"],
            client_secret=os.environ["GITHUB_CLIENT_SECRET"],
            base_url=os.environ["LENS_BASE_URL"],
            required_scopes=["read:user"],
            # Without a cache every MCP request makes GitHub API round-trips first.
            cache_ttl_seconds=GITHUB_TOKEN_CACHE_SECONDS,
        )
        # OAuthProxy verifies upstream GitHub tokens through ``_token_validator``;
        # wrapping it is how "any GitHub user" becomes the allowlist.
        provider._token_validator = AllowlistVerifier(provider._token_validator, logins)  # type: ignore[attr-defined]
        return provider
    raise RuntimeError(f"unknown LENS_MCP_AUTH {mode!r} (github | token | none)")


def _summary(photo: Photo) -> dict[str, Any]:
    return {
        "id": photo.id,
        "provider": photo.provider,
        "preview_url": photo.preview_url,
        "width": photo.width,
        "height": photo.height,
        "alt": photo.alt,
        "photographer": photo.photographer,
        "credit_markdown": photo.credit_markdown,
        "sizes": sorted(photo.downloads),
    }


async def _call(fn: Callable[[], T]) -> T:
    try:
        return await anyio.to_thread.run_sync(fn)
    except LensError as exc:
        raise ToolError(str(exc)) from exc


def build_mcp(auth: AuthProvider | None, *, cache: ResponseCache | None = None) -> FastMCP:
    mcp = FastMCP("lens", instructions=INSTRUCTIONS, version=__version__, auth=auth)
    cache = cache or ResponseCache()

    def source(provider: str | None) -> DirectSource:
        # Keys are resolved per call so rotated secrets apply without a restart.
        return DirectSource(provider or load_config().provider, cache=cache)

    @mcp.tool(annotations=READ)
    async def search_photos(
        query: Annotated[
            str, Field(min_length=1, max_length=100, description="What the photo shows.")
        ],
        orientation: Literal["landscape", "portrait", "square"] | None = None,
        page: Annotated[int, Field(ge=1)] = 1,
        limit: Annotated[int, Field(ge=1, le=40)] = 10,
        provider: ProviderName = None,
    ) -> dict[str, Any]:
        """Search free stock photos. Returns candidates with a small `preview_url`, size, alt
        tags, and a ready `credit_markdown`. Use `landscape` for hero images."""
        result = await _call(
            lambda: source(provider).search(query, page=page, limit=limit, orientation=orientation)
        )
        return {
            "photos": [_summary(photo) for photo in result.photos],
            "page": result.page,
            "total_results": result.total_results,
        }

    @mcp.tool(annotations=READ)
    async def view_photo(photo_id: PhotoId, provider: ProviderName = None) -> list[Any]:
        """Show a photo's medium-size preview so you can judge it before choosing."""

        def fetch() -> tuple[Photo, bytes, str]:
            photo = source(provider).get(photo_id)
            size = "medium" if "medium" in photo.downloads else "small"
            content, mime = fetch_image(photo, size)
            return photo, content, mime

        photo, content, mime = await _call(fetch)
        return [
            Image(data=content, format=mime.removeprefix("image/")),
            f"{photo.alt} ({photo.width}x{photo.height}). {photo.credit_markdown}",
        ]

    @mcp.tool(annotations=READ)
    async def get_photo(photo_id: PhotoId, provider: ProviderName = None) -> dict[str, Any]:
        """Full metadata for one photo, including `downloads` (a URL per size; `large` is a
        good default). Download the file promptly rather than hotlinking, and keep its
        `credit_markdown` with the image."""
        return asdict(await _call(lambda: source(provider).get(photo_id)))

    return mcp


def build_app() -> Starlette:
    mcp_app = build_mcp(build_auth()).http_app(path="/mcp")

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__})

    return Starlette(
        routes=[Route("/healthz", healthz), Mount("/", app=mcp_app)],
        lifespan=mcp_app.lifespan,
    )


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(build_app(), host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
