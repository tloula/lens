"""Download photo bytes safely and keep attribution in a ``<file>.photo.json`` sidecar."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image

from lens.models import LensError, Photo
from lens.providers import provider_class

MAX_IMAGE_BYTES = 25 * 1024 * 1024
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


@dataclass(frozen=True)
class Download:
    file: Path
    metadata: Path
    content_type: str
    sha256: str
    photo: Photo

    def summary(self) -> dict[str, object]:
        return {
            "file": str(self.file.resolve()),
            "metadata": str(self.metadata.resolve()),
            "content_type": self.content_type,
            "width": self.photo.width,
            "height": self.photo.height,
            "alt": self.photo.alt,
            "source_url": self.photo.source_url,
            "credit_markdown": self.photo.credit_markdown,
        }


def sidecar_path(file: Path) -> Path:
    return Path(f"{file}.photo.json")


def validate_image(content: bytes) -> str:
    """Return the MIME type of a JPEG, PNG, or WebP image, or raise ``LensError``."""
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise LensError("Photo must be between 1 byte and 25 MB.")
    try:
        with Image.open(BytesIO(content)) as image:
            mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(
                image.format or ""
            )
            image.verify()
    except (OSError, ValueError, Image.DecompressionBombError):
        mime = None
    if mime is None:
        raise LensError("Photo is not a valid JPEG, PNG, or WebP image.")
    return mime


def fetch_image(
    photo: Photo, size: str = "large", *, transport: httpx.BaseTransport | None = None
) -> tuple[bytes, str]:
    """Fetch one size from the provider's image CDN; returns ``(bytes, mime)``."""
    if size not in photo.downloads:
        raise LensError(f"Unknown size {size!r}; available: {', '.join(photo.downloads)}.")
    url = photo.downloads[size]
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in provider_class(photo.provider).image_hosts
    ):
        raise LensError("Provider returned an unexpected image URL.")
    content = bytearray()
    try:
        # A fresh client so no API key or backend token is ever sent to the image CDN.
        with (
            httpx.Client(timeout=60, transport=transport) as client,
            client.stream("GET", url) as response,
        ):
            if response.is_error:
                raise LensError(f"Image download failed (HTTP {response.status_code}).")
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > MAX_IMAGE_BYTES:
                    raise LensError("Photo exceeds the 25 MB download limit.")
    except httpx.HTTPError:
        raise LensError("Could not download the photo.") from None
    data = bytes(content)
    return data, validate_image(data)


def save_photo(
    photo: Photo,
    output: Path | None = None,
    *,
    size: str = "large",
    transport: httpx.BaseTransport | None = None,
) -> Download:
    """Download ``photo`` to ``output`` (a file, a directory, or the cwd) plus its sidecar.

    Existing files are never overwritten.
    """
    content, mime = fetch_image(photo, size, transport=transport)
    name = f"{photo.provider}-{photo.id}{EXTENSIONS[mime]}"
    file = Path(name) if output is None else output / name if output.is_dir() else output
    metadata = sidecar_path(file)
    if file.exists() or metadata.exists():
        raise LensError(f"{file} or its .photo.json already exists; choose another --output.")
    digest = hashlib.sha256(content).hexdigest()
    record = {"photo": asdict(photo), "size": size, "content_type": mime, "sha256": digest}
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("xb") as handle:
            handle.write(content)
        metadata.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except OSError as exc:
        raise LensError(f"Could not save photo: {exc}") from None
    return Download(file, metadata, mime, digest, photo)


def load_download(file: Path) -> tuple[Photo, bytes, str]:
    """Read a downloaded photo and verify it still matches its sidecar."""
    try:
        record = json.loads(sidecar_path(file).read_text(encoding="utf-8"))
        content = file.read_bytes()
        photo = Photo.from_dict(record["photo"])
        expected = record["sha256"]
    except (OSError, ValueError, KeyError, TypeError, LensError):
        raise LensError(f"Missing or invalid {sidecar_path(file).name}; download again.") from None
    if hashlib.sha256(content).hexdigest() != expected:
        raise LensError("Photo differs from its download metadata; download it again.")
    return photo, content, validate_image(content)
