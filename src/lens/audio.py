"""A shared stock audio library: uploaded tracks plus their metadata, kept in object storage.

Every track is two objects: ``audio/<id>.json`` (metadata) and ``audio/<id>.<ext>`` (the audio).
A track starts ``pending`` when its metadata is added and turns ``ready`` once its audio is
uploaded and checked, which also fills in the duration, size, and checksum.

Storage is an S3-compatible bucket when ``BUCKET_*`` is set (a Railway Storage Bucket in
production), otherwise a local directory for development.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, Protocol

from lens.models import LensError
from lens.paths import cache_dir

MAX_AUDIO_BYTES = 50 * 1024 * 1024
INDEX_TTL_SECONDS = 300
PREFIX = "audio/"
# Track ids become object keys and URL path segments.
TRACK_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
ENERGIES = ("low", "medium", "high")
Energy = Literal["low", "medium", "high"]

# mutagen class name -> (content type, extension)
FORMATS = {
    "MP3": ("audio/mpeg", ".mp3"),
    "MP4": ("audio/mp4", ".m4a"),
    "WAVE": ("audio/wav", ".wav"),
    "OggVorbis": ("audio/ogg", ".ogg"),
    "OggOpus": ("audio/ogg", ".opus"),
    "FLAC": ("audio/flac", ".flac"),
}


class Store(Protocol):
    def get(self, key: str) -> bytes | None: ...
    def put(self, key: str, content: bytes, content_type: str) -> None: ...
    def delete(self, key: str) -> None: ...
    def keys(self, prefix: str) -> list[str]: ...


class LocalStore:
    """A directory standing in for a bucket, for local development and tests."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / key

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def put(self, key: str, content: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, path)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def keys(self, prefix: str) -> list[str]:
        directory = self._path(prefix)
        if not directory.is_dir():
            return []
        return [
            prefix + path.name
            for path in directory.iterdir()
            if path.is_file() and not path.name.startswith(".")
        ]


class BucketStore:
    """An S3-compatible bucket, such as a Railway Storage Bucket."""

    def __init__(
        self,
        bucket: str,
        *,
        endpoint: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "auto",
    ) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(
                signature_version="s3v4",
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def get(self, key: str) -> bytes | None:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
        except self._client.exceptions.NoSuchKey:
            return None
        return response["Body"].read()

    def put(self, key: str, content: bytes, content_type: str) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=content, ContentType=content_type)

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def keys(self, prefix: str) -> list[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        return [
            item["Key"]
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix)
            for item in page.get("Contents", [])
        ]


def default_store() -> Store:
    bucket = os.environ.get("BUCKET_NAME")
    endpoint = os.environ.get("BUCKET_ENDPOINT")
    key_id = os.environ.get("BUCKET_ACCESS_KEY_ID")
    secret = os.environ.get("BUCKET_SECRET_ACCESS_KEY")
    if bucket and endpoint and key_id and secret:
        return BucketStore(
            bucket,
            endpoint=endpoint,
            access_key_id=key_id,
            secret_access_key=secret,
            region=os.environ.get("BUCKET_REGION") or "auto",
        )
    return LocalStore(cache_dir() / "library")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _words(*values: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", " ".join(values).lower()))


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist: str
    license: str
    source_url: str
    moods: list[str] = field(default_factory=list)
    genre: str = ""
    bpm: int | None = None
    energy: Energy | None = None
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    license_url: str = ""
    credit: str = ""
    status: Literal["pending", "ready"] = "pending"
    duration_seconds: float | None = None
    content_type: str = ""
    extension: str = ""
    size_bytes: int = 0
    sha256: str = ""
    added_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Track:
        known = {f.name for f in fields(cls)}
        try:
            track = cls(**{k: v for k, v in data.items() if k in known})
        except TypeError as exc:
            raise LensError("Invalid track metadata.") from exc
        if not TRACK_ID.fullmatch(track.id):
            raise LensError("Invalid track metadata.")
        return track

    @property
    def audio_key(self) -> str:
        return f"{PREFIX}{self.id}{self.extension}"

    @property
    def filename(self) -> str:
        return f"{self.id}{self.extension}"

    def summary(self) -> dict[str, Any]:
        """What a search result shows: everything but storage details."""
        data = asdict(self)
        for name in ("content_type", "extension", "sha256", "updated_at"):
            data.pop(name)
        return data

    def matches(self, terms: set[str]) -> bool:
        haystack = _words(self.title, self.artist, self.genre, self.notes, *self.moods, *self.tags)
        return terms <= haystack


def _clean_list(values: list[str] | None) -> list[str]:
    seen: dict[str, None] = {}
    for value in values or []:
        if cleaned := value.strip().lower():
            seen[cleaned] = None
    return list(seen)


def _check(track: Track) -> Track:
    if not track.title.strip():
        raise LensError("A track needs a title.", status=422)
    if not track.license.strip():
        raise LensError("A track needs its license, such as 'Pixabay Content License'.", status=422)
    if not track.source_url.startswith(("https://", "http://")):
        raise LensError("source_url must be the page the track came from.", status=422)
    if track.bpm is not None and not 20 <= track.bpm <= 300:
        raise LensError("bpm must be between 20 and 300.", status=422)
    if track.energy is not None and track.energy not in ENERGIES:
        raise LensError(f"energy must be one of {', '.join(ENERGIES)}.", status=422)
    return replace(track, moods=_clean_list(track.moods), tags=_clean_list(track.tags))


def probe_audio(content: bytes) -> tuple[str, str, float]:
    """Return ``(content_type, extension, duration_seconds)``, or raise ``LensError``."""
    import mutagen

    if not content or len(content) > MAX_AUDIO_BYTES:
        raise LensError("Audio must be between 1 byte and 50 MB.", status=413)
    try:
        audio = mutagen.File(BytesIO(content))  # pyright: ignore[reportPrivateImportUsage]
    except mutagen.MutagenError:  # pyright: ignore[reportPrivateImportUsage]
        audio = None
    kind = FORMATS.get(type(audio).__name__) if audio is not None else None
    length = getattr(getattr(audio, "info", None), "length", 0) or 0
    if kind is None or length <= 0:
        raise LensError("Audio must be an MP3, M4A, WAV, Ogg, Opus, or FLAC file.", status=415)
    return kind[0], kind[1], round(float(length), 2)


class AudioLibrary:
    """Track metadata and audio in a ``Store``, with an in-memory index of the metadata.

    The index reloads every few minutes so edits made straight in the bucket show up.
    """

    def __init__(self, store: Store | None = None) -> None:
        self.store = store or default_store()
        self._lock = threading.Lock()
        self._tracks: dict[str, Track] = {}
        self._loaded_at: float | None = None

    def _index(self) -> dict[str, Track]:
        with self._lock:
            if self._loaded_at is None or time.monotonic() - self._loaded_at > INDEX_TTL_SECONDS:
                tracks: dict[str, Track] = {}
                for key in self.store.keys(PREFIX):
                    if key.endswith(".json") and (raw := self.store.get(key)):
                        try:
                            track = Track.from_dict(json.loads(raw))
                        except (LensError, ValueError):
                            continue
                        tracks[track.id] = track
                self._tracks = tracks
                self._loaded_at = time.monotonic()
            return self._tracks

    def _save(self, track: Track) -> Track:
        track = replace(track, updated_at=_now())
        content = json.dumps(asdict(track), indent=2).encode()
        self.store.put(f"{PREFIX}{track.id}.json", content, "application/json")
        with self._lock:
            self._tracks[track.id] = track
        return track

    def get(self, track_id: str) -> Track:
        track = self._index().get(track_id) if TRACK_ID.fullmatch(track_id) else None
        if track is None:
            raise LensError(f"No track {track_id!r}.", status=404)
        return track

    def search(
        self,
        query: str = "",
        *,
        mood: str | None = None,
        energy: str | None = None,
        min_seconds: float | None = None,
        max_seconds: float | None = None,
        min_bpm: int | None = None,
        max_bpm: int | None = None,
        include_pending: bool = False,
    ) -> list[Track]:
        terms = _words(query)
        results = []
        for track in self._index().values():
            duration = track.duration_seconds or 0
            if (
                (track.status == "ready" or include_pending)
                and track.matches(terms)
                and (mood is None or mood.strip().lower() in track.moods)
                and (energy is None or track.energy == energy)
                and (min_seconds is None or duration >= min_seconds)
                and (max_seconds is None or duration <= max_seconds)
                and (min_bpm is None or (track.bpm is not None and track.bpm >= min_bpm))
                and (max_bpm is None or (track.bpm is not None and track.bpm <= max_bpm))
            ):
                results.append(track)
        return sorted(results, key=lambda track: track.added_at, reverse=True)

    def add(self, **metadata: Any) -> Track:
        slug = re.sub(r"[^a-z0-9]+", "-", str(metadata.get("title", "")).lower())[:60].strip("-")
        track_id = f"{slug or 'track'}-{secrets.token_hex(3)}"
        track = _check(Track(id=track_id, added_at=_now(), **metadata))
        return self._save(track)

    def update(self, track_id: str, **changes: Any) -> Track:
        track = _check(replace(self.get(track_id), **changes))
        return self._save(track)

    def upload(self, track_id: str, content: bytes) -> Track:
        """Store (or replace) a track's audio and mark it ready."""
        track = self.get(track_id)
        content_type, extension, duration = probe_audio(content)
        updated = replace(
            track,
            status="ready",
            content_type=content_type,
            extension=extension,
            duration_seconds=duration,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        self.store.put(updated.audio_key, content, content_type)
        if track.extension and track.audio_key != updated.audio_key:
            self.store.delete(track.audio_key)
        return self._save(updated)

    def audio(self, track_id: str) -> tuple[Track, bytes]:
        track = self.get(track_id)
        content = self.store.get(track.audio_key) if track.status == "ready" else None
        if content is None:
            raise LensError(f"Track {track_id!r} has no audio yet.", status=404)
        return track, content

    def delete(self, track_id: str) -> None:
        track = self.get(track_id)
        if track.extension:
            self.store.delete(track.audio_key)
        self.store.delete(f"{PREFIX}{track.id}.json")
        with self._lock:
            self._tracks.pop(track.id, None)
