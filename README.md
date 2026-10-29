# lens

Easy stock-photo and stock-music access for AI agents. One CLI searches, previews, and downloads photos with the attribution already worked out, so an agent can illustrate a blog post or a web page without extra setup. It also runs as a remote MCP server, so Claude can use it as a connector, and that server keeps a shared stock audio library. Pixabay is the default provider and Pexels is also supported. Every command prints JSON.

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

## MCP server

`lens-mcp` (`pip install "lens[mcp]"`) serves the same search as a remote MCP server at `/mcp`, with provider keys kept on the server. It has four read-only tools: `search_photos`, `view_photo` (returns the medium preview as an image so the model can judge it), `download_photo` (a one-hour signed link to `/download/...`, where this server fetches the image and serves it, so the client needs network access only to this server, not to Pixabay or Pexels), and `get_photo` (metadata with the provider's own download URLs). Download the chosen size right away rather than hotlinking it, and keep its `credit_markdown`.

| Variable | Purpose |
| --- | --- |
| `LENS_PIXABAY_API_KEY` / `PIXABAY_API_KEY` | Provider key, as for the CLI (`LENS_PEXELS_API_KEY` for Pexels). |
| `LENS_MCP_AUTH` | `github` (default; for claude.ai connectors), `token` (static bearer, for Claude Code or scripts), or `none` (local only). |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | A GitHub OAuth app whose callback URL is `https://<host>/auth/callback`. |
| `LENS_ALLOWED_GITHUB_LOGINS` | Comma-separated GitHub usernames allowed in. Everyone else is rejected. |
| `LENS_BASE_URL` | Public URL, such as `https://lens-production.up.railway.app`. |
| `LENS_MCP_TOKEN` | The bearer token for `LENS_MCP_AUTH=token` (at least 32 characters). |
| `LENS_DOWNLOAD_SECRET` | Optional key for signing download and upload links. Unset, a random key is made at startup, so links stop working after a restart. |
| `BUCKET_NAME`, `BUCKET_ENDPOINT`, `BUCKET_REGION`, `BUCKET_ACCESS_KEY_ID`, `BUCKET_SECRET_ACCESS_KEY` | The S3-compatible bucket for the audio library. Unset, tracks are kept under the cache directory. |
| `PORT` | Listen port (default 8000). |

### Audio library

The MCP server also keeps a shared library of stock music for videos and reels, so every project can reuse the same licensed tracks. Each track is stored in the bucket as its audio file plus a JSON record: title, artist, moods, genre, energy, bpm, tags, notes, license, source page, and credit, with the length, size, and checksum measured on upload.

| Tool | Purpose |
| --- | --- |
| `search_tracks` | Find tracks by words, mood, energy, length, or bpm |
| `list_tracks` | Every track (optionally including ones still waiting for audio) |
| `get_track` | All metadata for one track |
| `download_track` | A one-hour signed link to the audio file, served by this server |
| `add_track` | Record a track's metadata and get a one-hour signed `upload_url`; PUT the file there (`curl -fsS -X PUT --data-binary @song.mp3 '<upload_url>'`) |
| `upload_track` | A fresh upload link, to retry or replace a track's file |
| `update_track` / `delete_track` | Edit metadata, or remove a track |

Uploads accept MP3, M4A, WAV, Ogg, Opus, and FLAC up to 50 MB. A track appears in searches once its audio is uploaded. Always record the license and source page when adding a track.

Locally: `LENS_MCP_AUTH=none uv run --extra mcp lens-mcp`, then connect to `http://localhost:8000/mcp`.

**Deploy on Railway.** Create a service from this repo; the `Dockerfile` and `railway.toml` are picked up. Add a volume mounted at `/data`, which holds OAuth client registrations (`FASTMCP_HOME`) and the Pixabay response cache (`LENS_CACHE_DIR`). Add a Storage Bucket for the audio library and point the `BUCKET_*` variables at it with references such as `${{audio.BUCKET}}`. Generate a domain, create the GitHub OAuth app for it, and set the variables above. `/healthz` is the health check.

**Connect.** In claude.ai, open Settings, then Connectors, choose Add custom connector, and enter `https://<host>/mcp`; you sign in through GitHub. For Claude Code, run `claude mcp add --transport http lens https://<host>/mcp`.

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
