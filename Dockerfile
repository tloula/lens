FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra mcp --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra mcp

# /data is a Railway volume: OAuth client registrations and the Pixabay 24-hour response
# cache survive restarts.
ENV PATH="/app/.venv/bin:$PATH" \
    FASTMCP_HOME=/data/fastmcp \
    LENS_CACHE_DIR=/data/cache

CMD ["lens-mcp"]
