"""``lens``: search and download stock photos. Every command prints JSON."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import click

from lens import __version__
from lens.config import Config, backend_token, load_config
from lens.download import save_photo
from lens.models import LensError
from lens.paths import config_path
from lens.providers import PROVIDERS
from lens.secrets import delete_key, env_names, key_source, store_key
from lens.sources import DirectSource, RemoteSource, Source


def emit(value: Any) -> None:
    click.echo(json.dumps(value, indent=2, ensure_ascii=False))


class LensGroup(click.Group):
    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except LensError as exc:
            raise click.ClickException(str(exc)) from None


def provider_option(function: Any) -> Any:
    return click.option(
        "--provider",
        type=click.Choice(sorted(PROVIDERS)),
        help="Photo provider. Defaults to the configured provider (pixabay).",
    )(function)


def open_source(config: Config, provider: str | None) -> Source:
    name = provider or config.provider
    if config.backend:
        return RemoteSource(config.backend, name, token=backend_token(config))
    return DirectSource(name)


@click.group(cls=LensGroup)
@click.option(
    "--backend",
    metavar="URL",
    help="Use a lens-compatible photo API instead of local provider keys (or set LENS_BACKEND).",
)
@click.version_option(__version__, prog_name="lens")
@click.pass_context
def cli(ctx: click.Context, backend: str | None) -> None:
    """Find and download stock photos for agents.

    \b
    lens search "cozy coffee shop" --orientation landscape
    lens download <photo-id> --output hero.jpg
    """
    config = load_config()
    if backend:
        config = Config(config.provider, backend, config.token_command)
    ctx.obj = config


@cli.command()
@click.argument("query")
@provider_option
@click.option("--page", default=1, type=click.IntRange(1), show_default=True)
@click.option("--limit", default=12, type=click.IntRange(1, 80), show_default=True)
@click.option("--orientation", type=click.Choice(["landscape", "portrait", "square"]))
@click.pass_obj
def search(
    config: Config,
    query: str,
    provider: str | None,
    page: int,
    limit: int,
    orientation: str | None,
) -> None:
    """Search photos. Open `preview_url` to compare candidates before downloading."""
    result = open_source(config, provider).search(
        query, page=page, limit=limit, orientation=orientation
    )
    emit(asdict(result))


@cli.command()
@click.argument("photo_id")
@provider_option
@click.pass_obj
def get(config: Config, photo_id: str, provider: str | None) -> None:
    """Show one photo's metadata, sizes, and credit."""
    emit(asdict(open_source(config, provider).get(photo_id)))


@cli.command()
@click.argument("photo_id")
@provider_option
@click.option(
    "--output",
    help="File path, or a directory (existing or ending in /). Default: ./<provider>-<id>.<ext>",
)
@click.option(
    "--size",
    default="large",
    show_default=True,
    help="Key from the photo's `downloads` map (large, medium, small, original, ...).",
)
@click.pass_obj
def download(
    config: Config, photo_id: str, provider: str | None, output: str | None, size: str
) -> None:
    """Download a photo plus a <file>.photo.json sidecar holding its credit and checksum."""
    target = Path(output) if output else None
    if output and output.endswith(("/", "\\")):
        Path(output).mkdir(parents=True, exist_ok=True)
    photo = open_source(config, provider).get(photo_id)
    emit(save_photo(photo, target, size=size).summary())


@cli.group(cls=LensGroup)
def auth() -> None:
    """Manage provider API keys (environment variables take precedence)."""


@auth.command("status")
@click.pass_obj
def auth_status(config: Config) -> None:
    """Show where each key comes from and whether backend mode is on. Never prints keys."""
    emit(
        {
            "config_file": str(config_path()),
            "default_provider": config.provider,
            "backend": config.backend,
            "token_command": bool(config.token_command),
            "providers": {
                name: {"key": key_source(name), "env": list(env_names(name))}
                for name in sorted(PROVIDERS)
            },
        }
    )


@auth.command("set")
@click.argument("provider", type=click.Choice(sorted(PROVIDERS)))
@click.option("--stdin", "from_stdin", is_flag=True, help="Read the key from standard input.")
def auth_set(provider: str, from_stdin: bool) -> None:
    """Store a provider API key in the OS keyring."""
    key = sys.stdin.readline() if from_stdin else click.prompt("API key", hide_input=True)
    store_key(provider, key)
    emit({"provider": provider, "stored": "keyring"})


@auth.command("clear")
@click.argument("provider", type=click.Choice(sorted(PROVIDERS)))
def auth_clear(provider: str) -> None:
    """Remove a provider API key from the OS keyring."""
    emit({"provider": provider, "removed": delete_key(provider)})


def main() -> None:
    cli()
