"""Remote MCP server (``pip install lens[mcp]``): stock photos and a shared audio library as tools.

Run ``lens-mcp`` and connect a client to ``https://<host>/mcp``. Provider keys stay on the
server. Auth follows ``LENS_MCP_AUTH``: ``github`` (OAuth, for claude.ai connectors),
``token`` (a static bearer token, for Claude Code or scripts), or ``none`` (local only).
"""

# No ``from __future__ import annotations``: FastMCP reads the Annotated tool signatures.

import hashlib
import hmac
import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, TypeVar
from urllib.parse import urlsplit

import anyio.to_thread
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, AuthProvider, TokenVerifier
from fastmcp.utilities.types import Image
from pydantic import Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from lens import __version__
from lens.audio import MAX_AUDIO_BYTES, AudioLibrary, Energy, Track
from lens.cache import ResponseCache
from lens.config import load_config
from lens.download import EXTENSIONS, fetch_image
from lens.models import SAFE_ID, LensError, Photo
from lens.providers import PROVIDERS
from lens.sources import DirectSource

logger = logging.getLogger(__name__)

T = TypeVar("T")

READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}
LIBRARY_READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
LIBRARY_WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
LIBRARY_DELETE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}
GITHUB_TOKEN_CACHE_SECONDS = 300
DOWNLOAD_TTL_SECONDS = 60 * 60

INSTRUCTIONS = """lens finds free stock photos (Pixabay by default, Pexels too) with attribution
worked out. Workflow: `search_photos` for candidates, `view_photo` to look at the most promising
ones, then `download_photo` for a link that serves the full-size file through this server, so
you never need network access to the photo provider. Fetch it right away (links expire after an
hour) and save it to your own storage instead of hotlinking, and put the photo's
`credit_markdown` next to the image or in the page credits. Pick images whose subject is
obvious without explanation.

lens also keeps a shared stock audio library for videos and reels. `search_tracks` finds tracks
by words, mood, energy, length, or bpm (`list_tracks` shows everything), and `download_track`
gives a one-hour link to the file. To add a track, call `add_track` with its metadata and
license, then PUT the file to the returned `upload_url`. Use the track's `credit` wherever its
license asks for attribution."""

ProviderName = Annotated[
    str | None,
    Field(description=f"One of {sorted(PROVIDERS)}. Defaults to the server's provider (pixabay)."),
]
PhotoId = Annotated[str, Field(description="A photo `id` from search_photos.")]
TrackId = Annotated[str, Field(description="A track `id` from search_tracks or list_tracks.")]
Moods = Annotated[
    list[str] | None,
    Field(description="Mood words, such as upbeat, romantic, chill, spooky, cinematic."),
]


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


class DownloadSigner:
    """Short-lived HMAC-signed links: ``/download/...`` proxies photo bytes, and ``/audio/...``
    serves (GET) or receives (PUT) a library track. The method is part of the signature.

    The secret is ``LENS_DOWNLOAD_SECRET`` or random per process (links then die on restart,
    which is fine for one-hour links).
    """

    def __init__(
        self,
        secret: bytes | None = None,
        base_url: str | None = None,
        ttl: int = DOWNLOAD_TTL_SECONDS,
    ) -> None:
        env_secret = os.environ.get("LENS_DOWNLOAD_SECRET", "").encode()
        self._secret = secret or env_secret or secrets.token_bytes(32)
        port = os.environ.get("PORT", "8000")
        base = base_url or os.environ.get("LENS_BASE_URL") or f"http://localhost:{port}"
        self.base_url = base.rstrip("/")
        self.ttl = ttl

    def _signature(self, method: str, path: str, expires: int) -> str:
        message = f"{method} {path} {expires}".encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def sign_path(self, method: str, path: str) -> tuple[str, int]:
        expires = int(time.time()) + self.ttl
        signature = self._signature(method, path, expires)
        return f"{self.base_url}{path}?expires={expires}&sig={signature}", expires

    def verify_path(self, method: str, path: str, expires: str, signature: str) -> bool:
        if not expires.isdigit() or int(expires) < time.time():
            return False
        expected = self._signature(method, path, int(expires))
        return hmac.compare_digest(expected, signature)

    def sign(self, provider: str, photo_id: str, size: str) -> tuple[str, int]:
        return self.sign_path("GET", f"/download/{provider}/{photo_id}/{size}")

    def verify(self, provider: str, photo_id: str, size: str, expires: str, signature: str) -> bool:
        path = f"/download/{provider}/{photo_id}/{size}"
        return self.verify_path("GET", path, expires, signature)


def _extension(url: str) -> str:
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"


def _source(provider: str | None, cache: ResponseCache) -> DirectSource:
    # Keys are resolved per call so rotated secrets apply without a restart.
    return DirectSource(provider or load_config().provider, cache=cache)


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


def _track_summary(track: Track) -> dict[str, Any]:
    return {
        "id": track.id,
        "title": track.title,
        "artist": track.artist,
        "moods": track.moods,
        "genre": track.genre,
        "energy": track.energy,
        "bpm": track.bpm,
        "duration_seconds": track.duration_seconds,
        "license": track.license,
        "credit": track.credit,
        "status": track.status,
    }


async def _call(fn: Callable[[], T]) -> T:
    try:
        return await anyio.to_thread.run_sync(fn)
    except LensError as exc:
        raise ToolError(str(exc)) from exc


def build_mcp(
    auth: AuthProvider | None,
    *,
    cache: ResponseCache | None = None,
    signer: DownloadSigner | None = None,
    library: AudioLibrary | None = None,
) -> FastMCP:
    mcp = FastMCP("lens", instructions=INSTRUCTIONS, version=__version__, auth=auth)
    cache = cache or ResponseCache()
    signer = signer or DownloadSigner()
    library = library or AudioLibrary()

    def upload_link(track: Track) -> dict[str, Any]:
        url, expires = signer.sign_path("PUT", f"/audio/{track.id}")
        return {
            "upload_url": url,
            "upload_expires_at": expires,
            "upload_with": f"curl -fsS -X PUT --data-binary @<file> '{url}'",
        }

    def source(provider: str | None) -> DirectSource:
        return _source(provider, cache)

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
    async def download_photo(
        photo_id: PhotoId,
        size: Annotated[
            str, Field(description="A size from the photo's `sizes`; `large` suits most pages.")
        ] = "large",
        provider: ProviderName = None,
    ) -> dict[str, Any]:
        """A one-hour link that serves the image file from this server, so you only need
        network access to this server, not to the photo provider. Fetch it right away with
        something like `curl -fsSL -o <filename> '<download_url>'`, and keep the
        `credit_markdown` with the image."""
        photo = await _call(lambda: source(provider).get(photo_id))
        if size not in photo.downloads:
            raise ToolError(f"Unknown size {size!r}; available: {', '.join(photo.downloads)}.")
        url, expires = signer.sign(photo.provider, photo.id, size)
        return {
            "download_url": url,
            "expires_at": expires,
            "filename": f"{photo.provider}-{photo.id}{_extension(photo.downloads[size])}",
            "width": photo.width,
            "height": photo.height,
            "alt": photo.alt,
            "source_url": photo.source_url,
            "credit_markdown": photo.credit_markdown,
        }

    @mcp.tool(annotations=READ)
    async def get_photo(photo_id: PhotoId, provider: ProviderName = None) -> dict[str, Any]:
        """Full metadata for one photo, including provider `downloads` URLs (these need
        network access to the provider; use `download_photo` if you don't have it). Keep its
        `credit_markdown` with the image."""
        return asdict(await _call(lambda: source(provider).get(photo_id)))

    @mcp.tool(annotations=LIBRARY_READ)
    async def search_tracks(
        query: Annotated[
            str,
            Field(
                max_length=100,
                description="Words to match in title, artist, genre, moods, tags, or notes. "
                "Every word must match. Leave empty to filter only.",
            ),
        ] = "",
        mood: Annotated[str | None, Field(description="One mood the track must have.")] = None,
        energy: Energy | None = None,
        min_seconds: Annotated[float | None, Field(ge=0)] = None,
        max_seconds: Annotated[float | None, Field(ge=0)] = None,
        min_bpm: Annotated[int | None, Field(ge=0)] = None,
        max_bpm: Annotated[int | None, Field(ge=0)] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Search the shared stock audio library for tracks to put under videos and reels.
        Returns ready tracks with mood, energy, bpm, length, and license; newest first."""
        found = await _call(
            lambda: library.search(
                query,
                mood=mood,
                energy=energy,
                min_seconds=min_seconds,
                max_seconds=max_seconds,
                min_bpm=min_bpm,
                max_bpm=max_bpm,
            )
        )
        return {"tracks": [_track_summary(t) for t in found[:limit]], "total_results": len(found)}

    @mcp.tool(annotations=LIBRARY_READ)
    async def list_tracks(
        include_pending: Annotated[
            bool, Field(description="Also show tracks still waiting for their audio upload.")
        ] = False,
    ) -> dict[str, Any]:
        """Every track in the shared audio library, newest first."""
        found = await _call(lambda: library.search(include_pending=include_pending))
        return {"tracks": [_track_summary(t) for t in found], "total_results": len(found)}

    @mcp.tool(annotations=LIBRARY_READ)
    async def get_track(track_id: TrackId) -> dict[str, Any]:
        """All metadata for one track, including its source page, license, and checksum."""
        return asdict(await _call(lambda: library.get(track_id)))

    @mcp.tool(annotations=LIBRARY_READ)
    async def download_track(track_id: TrackId) -> dict[str, Any]:
        """A one-hour link that serves the track's audio file from this server. Fetch it
        right away with something like `curl -fsSL -o <filename> '<download_url>'`, and keep
        the `credit` with whatever you make if the license asks for attribution."""
        track = await _call(lambda: library.get(track_id))
        if track.status != "ready":
            raise ToolError(f"Track {track_id!r} has no audio yet; upload it first.")
        url, expires = signer.sign_path("GET", f"/audio/{track.id}")
        return {
            "download_url": url,
            "expires_at": expires,
            "filename": track.filename,
            "content_type": track.content_type,
            "duration_seconds": track.duration_seconds,
            "license": track.license,
            "credit": track.credit,
            "sha256": track.sha256,
        }

    @mcp.tool(annotations=LIBRARY_WRITE)
    async def add_track(
        title: Annotated[str, Field(min_length=1, max_length=200)],
        license: Annotated[
            str,
            Field(
                min_length=1,
                max_length=200,
                description="The license name, such as 'Pixabay Content License' or 'CC0'.",
            ),
        ],
        source_url: Annotated[
            str, Field(max_length=500, description="The page the track was downloaded from.")
        ],
        artist: Annotated[str, Field(max_length=200)] = "",
        moods: Moods = None,
        genre: Annotated[str, Field(max_length=100)] = "",
        energy: Energy | None = None,
        bpm: Annotated[int | None, Field(ge=20, le=300)] = None,
        tags: Annotated[
            list[str] | None, Field(description="Other words to search by, such as halloween.")
        ] = None,
        notes: Annotated[
            str, Field(max_length=1000, description="What it suits, best sections, and so on.")
        ] = "",
        license_url: Annotated[str, Field(max_length=500)] = "",
        credit: Annotated[
            str,
            Field(max_length=500, description="Attribution text, if the license asks for one."),
        ] = "",
    ) -> dict[str, Any]:
        """Add a track to the shared audio library. Returns the new track and an `upload_url`:
        PUT the audio file (MP3, M4A, WAV, Ogg, Opus, or FLAC, up to 50 MB) to it within an
        hour, for example `curl -fsS -X PUT --data-binary @song.mp3 '<upload_url>'`. The
        track shows up in searches once its audio is uploaded; its length is measured then."""
        track = await _call(
            lambda: library.add(
                title=title,
                artist=artist,
                license=license,
                source_url=source_url,
                moods=moods or [],
                genre=genre,
                energy=energy,
                bpm=bpm,
                tags=tags or [],
                notes=notes,
                license_url=license_url,
                credit=credit,
            )
        )
        return {"track": asdict(track), **upload_link(track)}

    @mcp.tool(annotations=LIBRARY_WRITE)
    async def upload_track(track_id: TrackId) -> dict[str, Any]:
        """A fresh one-hour `upload_url` for a track's audio: for a track whose first link
        expired, or to replace its file. PUT the file to it as with add_track."""
        track = await _call(lambda: library.get(track_id))
        return {"track": _track_summary(track), **upload_link(track)}

    @mcp.tool(annotations=LIBRARY_WRITE)
    async def update_track(
        track_id: TrackId,
        title: Annotated[str | None, Field(min_length=1, max_length=200)] = None,
        artist: Annotated[str | None, Field(max_length=200)] = None,
        license: Annotated[str | None, Field(min_length=1, max_length=200)] = None,
        source_url: Annotated[str | None, Field(max_length=500)] = None,
        moods: Annotated[list[str] | None, Field(description="Replaces the mood list.")] = None,
        genre: Annotated[str | None, Field(max_length=100)] = None,
        energy: Energy | None = None,
        bpm: Annotated[int | None, Field(ge=20, le=300)] = None,
        tags: Annotated[list[str] | None, Field(description="Replaces the tag list.")] = None,
        notes: Annotated[str | None, Field(max_length=1000)] = None,
        license_url: Annotated[str | None, Field(max_length=500)] = None,
        credit: Annotated[str | None, Field(max_length=500)] = None,
    ) -> dict[str, Any]:
        """Change a track's metadata. Only the fields you pass change."""
        changes = {
            name: value
            for name, value in {
                "title": title,
                "artist": artist,
                "license": license,
                "source_url": source_url,
                "moods": moods,
                "genre": genre,
                "energy": energy,
                "bpm": bpm,
                "tags": tags,
                "notes": notes,
                "license_url": license_url,
                "credit": credit,
            }.items()
            if value is not None
        }
        return asdict(await _call(lambda: library.update(track_id, **changes)))

    @mcp.tool(annotations=LIBRARY_DELETE)
    async def delete_track(track_id: TrackId) -> dict[str, Any]:
        """Remove a track and its audio from the library for everyone. Cannot be undone."""
        await _call(lambda: library.delete(track_id))
        return {"deleted": track_id}

    return mcp


def build_app(
    *,
    cache: ResponseCache | None = None,
    signer: DownloadSigner | None = None,
    library: AudioLibrary | None = None,
) -> Starlette:
    cache = cache or ResponseCache()
    signer = signer or DownloadSigner()
    library = library or AudioLibrary()
    mcp = build_mcp(build_auth(), cache=cache, signer=signer, library=library)
    mcp_app = mcp.http_app(path="/mcp")

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__})

    async def download(request: Request) -> Response:
        provider = request.path_params["provider"]
        photo_id = request.path_params["photo_id"]
        size = request.path_params["size"]
        valid = (
            provider in PROVIDERS
            and SAFE_ID.fullmatch(photo_id)
            and SAFE_ID.fullmatch(size)
            and signer.verify(
                provider,
                photo_id,
                size,
                request.query_params.get("expires", ""),
                request.query_params.get("sig", ""),
            )
        )
        if not valid:
            return JSONResponse({"error": "Invalid or expired download link."}, status_code=403)

        def fetch() -> tuple[Photo, bytes, str]:
            photo = _source(provider, cache).get(photo_id)
            content, mime = fetch_image(photo, size)
            return photo, content, mime

        try:
            photo, content, mime = await anyio.to_thread.run_sync(fetch)
        except LensError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        filename = f"{photo.provider}-{photo.id}{EXTENSIONS[mime]}"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, max-age=3600",
        }
        return Response(content, media_type=mime, headers=headers)

    async def audio(request: Request) -> Response:
        track_id = request.path_params["track_id"]
        path = f"/audio/{track_id}"
        if not signer.verify_path(
            request.method,
            path,
            request.query_params.get("expires", ""),
            request.query_params.get("sig", ""),
        ):
            return JSONResponse({"error": "Invalid or expired link."}, status_code=403)
        try:
            if request.method == "PUT":
                declared = request.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > MAX_AUDIO_BYTES:
                    raise LensError("Audio must be between 1 byte and 50 MB.", status=413)
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > MAX_AUDIO_BYTES:
                        raise LensError("Audio must be between 1 byte and 50 MB.", status=413)
                content = bytes(body)
                track = await anyio.to_thread.run_sync(lambda: library.upload(track_id, content))
                return JSONResponse({"track": _track_summary(track)})
            track, content = await anyio.to_thread.run_sync(lambda: library.audio(track_id))
        except LensError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        headers = {
            "Content-Disposition": f'attachment; filename="{track.filename}"',
            "Cache-Control": "private, max-age=3600",
        }
        return Response(content, media_type=track.content_type, headers=headers)

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/download/{provider}/{photo_id}/{size}", download),
            Route("/audio/{track_id}", audio, methods=["GET", "PUT"]),
            Mount("/", app=mcp_app),
        ],
        lifespan=mcp_app.lifespan,
    )


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(build_app(), host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
