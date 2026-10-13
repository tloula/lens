"""Non-secret settings from ``config.toml`` with ``LENS_*`` environment overrides."""

from __future__ import annotations

import os
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass

from lens.models import LensError
from lens.paths import config_path
from lens.providers import DEFAULT_PROVIDER


@dataclass(frozen=True)
class Config:
    provider: str = DEFAULT_PROVIDER
    # A lens-compatible photo API, e.g. https://api.example.com/photos. Unset = call providers directly.
    backend: str | None = None
    # Shell command printing a bearer token for ``backend`` (like a git credential helper).
    token_command: str | None = None


def load_config() -> Config:
    path = config_path()
    data: dict[str, object] = {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LensError(f"Could not read {path}: {exc}") from None

    def setting(name: str) -> str | None:
        value = os.environ.get(f"LENS_{name.upper()}") or data.get(name)
        if value is not None and not isinstance(value, str):
            raise LensError(f"{path}: {name} must be a string.")
        return value or None

    return Config(
        provider=setting("provider") or DEFAULT_PROVIDER,
        backend=setting("backend"),
        token_command=setting("token_command"),
    )


def backend_token(config: Config) -> Callable[[], str] | None:
    """Return a token getter for backend mode, or None when the backend needs no auth."""
    if token := os.environ.get("LENS_BACKEND_TOKEN", "").strip():
        return lambda: token
    command = config.token_command
    if not command:
        return None
    cached: list[str] = []

    def run() -> str:
        if not cached:
            try:
                result = subprocess.run(
                    command, shell=True, capture_output=True, text=True, timeout=120, check=True
                )
            except (OSError, subprocess.SubprocessError):
                raise LensError("token_command failed; run it directly to see why.") from None
            if not (value := result.stdout.strip()):
                raise LensError("token_command printed no token.")
            cached.append(value)
        return cached[0]

    return run
