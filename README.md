# lens

Easy stock-photo access for AI agents. One CLI searches, previews, and downloads photos with the attribution already worked out, so an agent can illustrate a blog post or a web page without extra setup or an MCP server. Pixabay is the default provider and Pexels is also supported. Every command prints JSON.

## Quick start

With a clone of this repo and a [Pixabay API key](https://pixabay.com/api/docs/) (free):

```sh
export PIXABAY_API_KEY=...        # PowerShell: $env:PIXABAY_API_KEY = "..."
uv run lens search "cozy coffee shop" --orientation landscape --limit 5
uv run lens download 8352847 --output images/hero.jpg
```

This needs [uv](https://docs.astral.sh/uv/) (`pip install uv`) and Python 3.11 or newer; `uv run` builds the environment on first use. Without uv, run `pip install .` and then call `lens` directly. To install it for use from any directory, run `uv tool install git+https://github.com/tloula/lens`.

## Agent workflow

1. **Search.** `lens search "<subject>"` returns `{photos, page, total_results}`. Each photo has an `id`, a `preview_url`, its `width` and `height`, `alt` tags, the `photographer`, a `credit_markdown` line, and a `downloads` map of sizes. Use `--page` for more results and `--orientation landscape|portrait` for hero images.
2. **Choose.** View the `preview_url` of a few candidates. Pick an image whose subject is obvious without any explanation.
3. **Download.** `lens download <id> --output path/name.jpg` saves the image and a `name.jpg.photo.json` sidecar that holds the source, credit, and checksum. It prints the file path and `credit_markdown`. The default size is `large`; `--size` accepts any key from `downloads`. Without `--output`, the file is saved as `./<provider>-<id>.<ext>`. Existing files are never overwritten.
4. **Use.** Reference the downloaded file and put `credit_markdown` near the image or in the page's credits. Do not hotlink provider URLs, and never commit API keys.

| Command | Purpose |
| --- | --- |
| `lens search QUERY [--provider] [--page] [--limit] [--orientation]` | Find candidates |
| `lens get ID [--provider]` | Show metadata for one photo |
| `lens download ID [--provider] [--output] [--size]` | Save the image and its attribution sidecar |
| `lens auth status` | Show where keys come from (never prints them) |
| `lens auth set PROVIDER` / `lens auth clear PROVIDER` | Store or remove a key in the OS keyring |

Use the same `--provider` for `search` and `download`.

## API keys

lens checks these sources in order, for each provider:

1. `LENS_PIXABAY_API_KEY` (or `LENS_PEXELS_API_KEY`)
2. `PIXABAY_API_KEY` (or `PEXELS_API_KEY`)
3. The OS keyring (Windows Credential Manager, macOS Keychain, Secret Service), set with `lens auth set pixabay`

Use environment variables in cloud sandboxes and CI, or inject them with a secret manager (`doppler run -- lens ...`, `op run -- lens ...`). On a workstation, the keyring keeps the key out of shell history and dotfiles. Keys are never written to config files or to the response cache. Pixabay's API terms require responses to be cached for 24 hours; lens stores that cache privately under the user cache directory (override it with `LENS_CACHE_DIR`).

## Backend mode

An app that already holds provider keys on its server can serve lens routes, and clients then need only that app's login:

```python
from fastapi import Depends
from lens.server import create_router  # pip install "lens[server]"

app.include_router(
    create_router(lambda: {"pixabay": settings.pixabay_key}, dependencies=[Depends(require_user)]),
    prefix="/api",
)
```

Provider failures return HTTP 404, 422, 429, or 424 rather than 502, because CDNs such as Cloudflare replace 502 responses with their own page. In containers, set `LENS_CACHE_DIR` to a writable directory, or pass `cache=ResponseCache(path)`, so the Pixabay cache works.

Point the CLI at it with `--backend https://app.example.com/api/photos` or a config file. The config file is `~/.config/lens/config.toml`, or `%APPDATA%\lens\config.toml` on Windows (override it with `LENS_CONFIG`):

```toml
provider = "pixabay"
backend = "https://app.example.com/api/photos"
token_command = "my-app auth token"   # prints a bearer token, like a git credential helper
```

`LENS_BACKEND`, `LENS_PROVIDER`, `LENS_TOKEN_COMMAND`, and `LENS_BACKEND_TOKEN` override the file. The config file holds only non-secret settings.

## Library

```python
from pathlib import Path

from lens.download import save_photo
from lens.sources import DirectSource, RemoteSource

source = DirectSource("pixabay")  # or RemoteSource(url, "pixabay", token=get_token)
photo = source.search("bakery storefront", limit=3).photos[0]
print(save_photo(photo, Path("images")).summary()["credit_markdown"])
```

`lens.download.load_download(path)` reads a downloaded file back and verifies it against its sidecar before you upload it elsewhere.

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

To add a provider, create a module in `src/lens/providers/` that implements the `Provider` protocol, and register it in `providers/__init__.py`.
