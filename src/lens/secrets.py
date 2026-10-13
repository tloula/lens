"""Provider API keys: environment variables first, then the OS keyring. Never plaintext config."""

from __future__ import annotations

import os

from lens.models import LensError
from lens.providers import provider_class

KEYRING_SERVICE = "lens"


def env_names(provider: str) -> tuple[str, str]:
    name = provider.upper()
    return f"LENS_{name}_API_KEY", f"{name}_API_KEY"


def _keyring_get(provider: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, provider)
    except Exception:
        return None  # No usable keyring backend (common in containers and CI).


def key_source(provider: str) -> str | None:
    """Describe where the key comes from, without revealing it."""
    for name in env_names(provider):
        if os.environ.get(name, "").strip():
            return f"env:{name}"
    return "keyring" if _keyring_get(provider) else None


def resolve_key(provider: str) -> str:
    label = provider_class(provider).label
    for name in env_names(provider):
        if value := os.environ.get(name, "").strip():
            return value
    if value := _keyring_get(provider):
        return value
    primary, alternate = env_names(provider)
    raise LensError(
        f"No {label} API key found. Set {primary} (or {alternate}), "
        f"or run `lens auth set {provider}` to store it in the OS keyring."
    )


def store_key(provider: str, key: str) -> None:
    provider_class(provider)
    if not key.strip():
        raise LensError("API key must not be empty.")
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, provider, key.strip())
    except Exception:
        raise LensError(
            f"No OS keyring is available here; set {env_names(provider)[0]} instead."
        ) from None


def delete_key(provider: str) -> bool:
    provider_class(provider)
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, provider)
        return True
    except Exception:
        return False
