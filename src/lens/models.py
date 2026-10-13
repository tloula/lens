"""Provider-neutral photo metadata shared by every source and the HTTP API."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any


class LensError(RuntimeError):
    """A failure whose message is safe to show to users and agents (never contains secrets)."""


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
