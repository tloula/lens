from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner
from conftest import jpeg, pixabay_hit

from lens.cli import cli
from lens.providers.pixabay import PixabayProvider


def test_search_and_download_with_env_key(
    mock_http, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LENS_PIXABAY_API_KEY", "cli-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pixabay.com" and request.url.path == "/api/":
            return httpx.Response(200, json={"hits": [pixabay_hit()], "totalHits": 1})
        assert "cli-key" not in str(request.url)
        return httpx.Response(200, content=jpeg())

    mock_http(handler)
    runner = CliRunner()
    searched = runner.invoke(cli, ["search", "door", "--limit", "1"])
    assert searched.exit_code == 0, searched.output
    assert json.loads(searched.output)["photos"][0]["id"] == "123"
    assert "cli-key" not in searched.output

    output = tmp_path / "hero.jpg"
    downloaded = runner.invoke(cli, ["download", "123", "--output", str(output)])
    assert downloaded.exit_code == 0, downloaded.output
    summary = json.loads(downloaded.output)
    assert Path(summary["file"]) == output.resolve()
    assert summary["credit_markdown"].startswith("Photo by [Sample Person]")
    assert output.exists() and Path(f"{output}.photo.json").exists()


def test_missing_key_is_a_clean_error() -> None:
    result = CliRunner().invoke(cli, ["search", "door"])
    assert result.exit_code == 1
    assert "PIXABAY_API_KEY" in result.output
    assert "Traceback" not in result.output


def test_backend_mode_uses_token_command(mock_http, tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    command = f'"{sys.executable}" -c "print(\'from-command\')"'
    config.write_text(
        f"backend = 'https://api.example.com/photos'\ntoken_command = '''{command}'''\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/photos/pixabay/123"
        assert request.headers["Authorization"] == "Bearer from-command"
        return httpx.Response(200, json=asdict(PixabayProvider._photo(pixabay_hit())))

    mock_http(handler)
    result = CliRunner().invoke(cli, ["get", "123"], env={"LENS_CONFIG": str(config)})
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["id"] == "123"


def test_auth_status_never_prints_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIXABAY_API_KEY", "status-key")
    result = CliRunner().invoke(cli, ["auth", "status"])
    assert result.exit_code == 0
    assert json.loads(result.output)["providers"]["pixabay"]["key"] == "env:PIXABAY_API_KEY"
    assert "status-key" not in result.output


def test_download_to_new_directory(
    mock_http, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PIXABAY_API_KEY", "k")
    mock_http(
        lambda request: (
            httpx.Response(200, json={"hits": [pixabay_hit()], "totalHits": 1})
            if request.url.path == "/api/"
            else httpx.Response(200, content=jpeg())
        )
    )
    result = CliRunner().invoke(cli, ["download", "123", "--output", f"{tmp_path / 'new'}/"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "new" / "pixabay-123.jpg").exists()
