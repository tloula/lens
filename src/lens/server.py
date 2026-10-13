"""FastAPI routes for backend mode (``pip install lens[server]``); keys never leave the server.

Mount the router in any app and point ``lens --backend <app-origin><prefix>`` at it::

    app.include_router(create_router(lambda: {"pixabay": settings.pixabay_key}), prefix="/api")
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from lens.cache import ResponseCache
from lens.models import LensError, Photo, SearchResult
from lens.providers import PROVIDERS, Provider, create_provider


def create_router(
    keys: Callable[[], Mapping[str, str | None]],
    *,
    dependencies: Sequence[Any] = (),
    prefix: str = "/photos",
    cache: ResponseCache | None = None,
) -> APIRouter:
    """Serve ``GET {prefix}/{provider}/search`` and ``GET {prefix}/{provider}/{photo_id}``.

    ``keys`` is called per request so rotated secrets apply without a restart; put
    authentication in ``dependencies`` (for example ``[Depends(require_user)]``).
    """
    router = APIRouter(prefix=prefix, tags=["photos"], dependencies=list(dependencies))

    def provider(name: str) -> Provider:
        if name not in PROVIDERS:
            raise HTTPException(status_code=404, detail="Unknown photo provider.")
        return create_provider(name, keys().get(name), cache=cache)

    @router.get("/{provider_name}/search")
    async def search_photos(
        provider_name: str,
        query: str = Query(min_length=1, max_length=100),
        page: int = Query(default=1, ge=1),
        limit: int = Query(default=12, ge=1, le=80),
        orientation: Literal["landscape", "portrait", "square"] | None = None,
    ) -> SearchResult:
        client = provider(provider_name)
        try:
            return await run_in_threadpool(
                client.search, query, page=page, limit=limit, orientation=orientation
            )
        except LensError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @router.get("/{provider_name}/{photo_id}")
    async def get_photo(provider_name: str, photo_id: str) -> Photo:
        client = provider(provider_name)
        try:
            return await run_in_threadpool(client.get, photo_id)
        except LensError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    return router
