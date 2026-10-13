"""Provider-neutral photo metadata shared by every source and the HTTP API."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Any

# Photo IDs become URL path segments and default file names, so keep them inert.
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")


class LensError(RuntimeError):
    """A failure whose message is safe to show to users and agents (never contains secrets).

    ``status`` is the HTTP status ``lens.server`` responds with. The default is 424 rather
    than 502 because CDNs such as Cloudflare replace origin 502 bodies with their own page.
    """

    def __init__(self, message: str, *, status: int = 424) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Photo:
    provider: str
    id: str
    source_url: str
    preview_url: str
    width: int
    height: int
    alt: str
    photographer: str
    credit_markdown: str
    downloads: dict[str, str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Photo:
        try:
            values: dict[str, Any] = {f.name: str(data[f.name]) for f in fields(cls)}
            values["width"], values["height"] = int(data["width"]), int(data["height"])
            values["downloads"] = {str(k): str(v) for k, v in dict(data["downloads"]).items()}
        except (KeyError, TypeError, ValueError) as exc:
            raise LensError("Invalid photo metadata.") from exc
        if not SAFE_ID.fullmatch(values["id"]) or not SAFE_ID.fullmatch(values["provider"]):
            raise LensError("Invalid photo metadata.")
        return cls(**values)


@dataclass(frozen=True)
class SearchResult:
    photos: list[Photo]
    page: int
    total_results: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SearchResult:
        try:
            return cls(
                [Photo.from_dict(photo) for photo in data["photos"]],
                int(data["page"]),
                int(data["total_results"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LensError("Invalid photo search response.") from exc
