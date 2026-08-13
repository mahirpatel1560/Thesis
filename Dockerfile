# The Discord league bot, for always-on hosting on a small VPS.
#
# Two things this file is careful about:
#
#   * No secrets. Nothing is COPYied that could contain a credential (see
#     .dockerignore) and no ARG or ENV carries one. DISCORD_TOKEN and
#     ANTHROPIC_API_KEY arrive from the environment at run time, so the image
#     stays safe to rebuild, push, or hand to someone.
#   * No state. Every path the bot writes to lives under /data, which is a mounted
#     volume, so league.db, arena.db and the cached briefs all survive a rebuild.
#     A brief costs a model call; losing the cache on every deploy would be
#     paying twice for the same paragraph.

FROM python:3.12-slim-bookworm

# Pinned deliberately: `latest` would make an image rebuild a different build.
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
# PYTHONUNBUFFERED matters more than it looks: without it the log lines that make
# a live failure diagnosable sit in a buffer instead of reaching `docker logs`.

WORKDIR /app

# Dependencies first, as their own layer: they change far less often than the
# source, so an ordinary code deploy reuses this and rebuilds in seconds.
# CLAUDE.md is here because pyproject.toml declares it as the readme, and the
# build backend reads it.
COPY pyproject.toml uv.lock CLAUDE.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
RUN uv sync --frozen --no-dev

# Run as a normal user. The uid is fixed so file ownership on the volume stays
# stable across rebuilds.
RUN useradd --create-home --uid 10001 thesis

# /data is created here, owned by `thesis`, *before* the volume is declared:
# Docker seeds a fresh named volume from the image's directory, ownership
# included, so the unprivileged user can write to it without a startup chown.
RUN mkdir -p /data/cache /data/briefs /data/reports && chown -R thesis:thesis /data
VOLUME ["/data"]

ENV THESIS_CACHE_DIR=/data/cache \
    THESIS_BRIEFS_DIR=/data/briefs \
    THESIS_REPORTS_DIR=/data/reports \
    PATH="/app/.venv/bin:$PATH"

USER thesis

# The console script straight from the venv — no uv resolution at container
# start, so the bot is connecting to Discord a moment sooner.
CMD ["thesis", "bot"]
