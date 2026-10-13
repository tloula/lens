"""Registered photo providers. Add a module here to support a new source."""

from __future__ import annotations

import httpx

from lens.cache import ResponseCache
from lens.models import LensError
from lens.providers.base import Provider
from lens.providers.pexels import PexelsProvider
from lens.providers.pixabay import PixabayProvider

PROVIDERS: dict[str, type[PixabayProvider] | type[PexelsProvider]] = {
    PixabayProvider.name: PixabayProvider,
    PexelsProvider.name: PexelsProvider,
}
DEFAULT_PROVIDER = PixabayProvider.name


def provider_class(name: str) -> type[PixabayProvider] | type[PexelsProvider]:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise LensError(
            f"Unknown photo provider {name!r}; choose from {', '.join(PROVIDERS)}."
        ) from None


def create_provider(
    name: str,
    api_key: str | None,
    *,
    cache: ResponseCache | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Provider:
    return provider_class(name)(api_key, cache=cache, transport=transport)


__all__ = ["DEFAULT_PROVIDER", "PROVIDERS", "Provider", "create_provider", "provider_class"]
