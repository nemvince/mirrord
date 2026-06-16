FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    rsync \
    curl \
    openssl \
    debmirror \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=oven/bun:latest /usr/local/bin/bun /usr/local/bin/bun

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project

COPY package.json bun.lock ./
RUN bun install --frozen-lockfile

COPY app/ app/

RUN bun tailwindcss -i app/static/tailwind.css -o app/static/output.css --minify \
    && rm -rf node_modules /usr/local/bin/bun

RUN mkdir -p /data /var/lock/mirrord

EXPOSE 8080

ENV MIRRORD_CONFIG=config.yaml

CMD ["uv", "run", "python", "-m", "app.main"]
