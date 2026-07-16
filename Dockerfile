# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv for fast, reproducible installs from the committed lockfile.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (cached layer) using only the lock + manifest.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Then the app itself.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Seed the profiles into the image as a default so the container works without
# a config mount; compose mounts ./config:/config (read-only) to override.
COPY config /config

# Volume-mounted state (see compose): DB, profiles, cached stickers.
ENV CARFINDER_DB_PATH=/data/carfinder.sqlite \
    CARFINDER_STICKERS_DIR=/stickers \
    CARFINDER_CONFIG_DIR=/config \
    PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

# Serve the UI + scheduler, bound to all interfaces inside the container.
CMD ["carfinder", "serve", "--host", "0.0.0.0", "--port", "8000"]
