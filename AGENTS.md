# Agent Instructions

lens is a stock-photo CLI for agents. Read `README.md` before using or changing it.

- To get photos for a task, run `uv run lens search "<subject>"`, view the candidates' `preview_url`, then run `uv run lens download <id> --output <path>`. Use the printed `credit_markdown` next to the image.
- For music under a video, use the MCP server's audio library: `search_tracks`, then `download_track`. Add new tracks with `add_track` and always record their license and source page.
- Keys come from `PIXABAY_API_KEY` / `LENS_PIXABAY_API_KEY` or the OS keyring. Run `uv run lens auth status` if a key is missing. Never print or commit keys.
- Before committing changes, run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pyright`.
