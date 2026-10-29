from __future__ import annotations

import wave
from io import BytesIO
from pathlib import Path
from typing import Any

import boto3
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from moto import mock_aws
from starlette.testclient import TestClient

from lens.audio import AudioLibrary, BucketStore, LocalStore
from lens.mcp_server import DownloadSigner, build_app, build_mcp
from lens.models import LensError

pytestmark = pytest.mark.anyio
BASE = "https://lens.example.com"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def wav(seconds: float = 2.0) -> bytes:
    stream = BytesIO()
    with wave.open(stream, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(8000)
        file.writeframes(b"\0\0" * int(8000 * seconds))
    return stream.getvalue()


def metadata(**overrides: Any) -> dict[str, Any]:
    return {
        "title": "Upbeat Indie Rock",
        "artist": "echobrainz",
        "license": "Pixabay Content License",
        "source_url": "https://pixabay.com/music/indie-rock-123/",
        "moods": ["Upbeat", "happy", "upbeat"],
        "energy": "high",
        "bpm": 128,
        **overrides,
    }


@pytest.fixture
def library(tmp_path: Path) -> AudioLibrary:
    return AudioLibrary(LocalStore(tmp_path / "bucket"))


def test_tracks_are_pending_until_audio_is_uploaded(library: AudioLibrary) -> None:
    track = library.add(**metadata())
    assert track.id.startswith("upbeat-indie-rock-")
    assert track.moods == ["upbeat", "happy"]
    assert library.search() == []
    assert library.search(include_pending=True) == [track]

    ready = library.upload(track.id, wav(2.5))
    assert ready.status == "ready"
    assert ready.duration_seconds == 2.5
    assert ready.extension == ".wav"
    assert library.search("indie") == [ready]
    assert library.audio(track.id)[1] == wav(2.5)


def test_search_filters(library: AudioLibrary) -> None:
    rock = library.upload(library.add(**metadata()).id, wav(3))
    calm = library.upload(
        library.add(**metadata(title="Lay Back", moods=["chill"], energy="low", bpm=80)).id,
        wav(1),
    )
    assert library.search(mood="CHILL") == [calm]
    assert library.search(energy="high") == [rock]
    assert library.search(min_seconds=2) == [rock]
    assert library.search(max_bpm=100) == [calm]
    assert library.search("rock upbeat") == [rock]
    assert library.search("rock chill") == []


def test_metadata_survives_a_restart_and_rejects_bad_input(library: AudioLibrary) -> None:
    track = library.add(**metadata())
    library.update(track.id, notes="Great for the drop at 0:12")
    fresh = AudioLibrary(library.store)
    assert fresh.get(track.id).notes == "Great for the drop at 0:12"
    with pytest.raises(LensError, match="license"):
        library.add(**metadata(license=" "))
    with pytest.raises(LensError, match="source_url"):
        library.update(track.id, source_url="file:///etc/passwd")
    with pytest.raises(LensError, match="MP3"):
        library.upload(track.id, b"not audio")
    with pytest.raises(LensError, match="No track"):
        library.get("../secrets")
    library.store.put("audio/bad.json", b'{"id": "bad", "bpm": "fast"}', "application/json")
    assert AudioLibrary(library.store).search(include_pending=True)[0].id == track.id


def test_delete_removes_audio_and_metadata(library: AudioLibrary) -> None:
    track = library.upload(library.add(**metadata()).id, wav())
    library.delete(track.id)
    assert library.store.keys("audio/") == []
    with pytest.raises(LensError):
        library.get(track.id)


def test_bucket_store_round_trip() -> None:
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="audio")
        store = BucketStore(
            "audio",
            endpoint="https://s3.amazonaws.com",
            access_key_id="test",
            secret_access_key="test",
            region="us-east-1",
        )
        library = AudioLibrary(store)
        track = library.upload(library.add(**metadata()).id, wav())
        assert sorted(store.keys("audio/")) == [f"audio/{track.id}.json", f"audio/{track.id}.wav"]
        assert AudioLibrary(store).audio(track.id)[1] == wav()
        assert store.get("audio/missing.json") is None


async def test_mcp_add_upload_search_download(
    library: AudioLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LENS_MCP_AUTH", "none")
    signer = DownloadSigner(b"s" * 32, base_url=BASE)
    async with Client(build_mcp(None, signer=signer, library=library)) as client:
        added = (await client.call_tool("add_track", metadata())).data
        track_id = added["track"]["id"]
        assert added["upload_url"].startswith(f"{BASE}/audio/{track_id}?")
        with pytest.raises(ToolError, match="no audio yet"):
            await client.call_tool("download_track", {"track_id": track_id})

        with TestClient(build_app(signer=signer, library=library)) as http:
            upload = added["upload_url"].removeprefix(BASE)
            assert http.put(upload.replace("sig=", "sig=0"), content=wav()).status_code == 403
            response = http.put(upload, content=b"not audio")
            assert response.status_code == 415
            response = http.put(upload, content=wav())
            assert response.status_code == 200
            assert response.json()["track"]["duration_seconds"] == 2.0
            # Upload links are single-use: once the audio changes, the old link is refused.
            assert http.put(upload, content=wav(1)).status_code == 409
            fresh = (await client.call_tool("upload_track", {"track_id": track_id})).data
            response = http.put(fresh["upload_url"].removeprefix(BASE), content=wav())
            assert response.status_code == 200
            # An upload link can't be used to download, and the reverse.
            assert http.get(upload).status_code == 403

            found = (await client.call_tool("search_tracks", {"mood": "upbeat"})).data
            assert [t["id"] for t in found["tracks"]] == [track_id]
            listed = (await client.call_tool("list_tracks", {})).data
            assert listed["total_results"] == 1

            link = (await client.call_tool("download_track", {"track_id": track_id})).data
            assert link["filename"] == f"{track_id}.wav"
            response = http.get(link["download_url"].removeprefix(BASE))
            assert response.status_code == 200
            assert response.headers["content-type"] == "audio/wav"
            assert response.content == wav()

        updated = (
            await client.call_tool("update_track", {"track_id": track_id, "genre": "rock"})
        ).data
        assert updated["genre"] == "rock"
        assert updated["bpm"] == 128
        await client.call_tool("delete_track", {"track_id": track_id})
        assert (await client.call_tool("list_tracks", {})).data["total_results"] == 0
