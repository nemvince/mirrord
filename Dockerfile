FROM oven/bun:alpine AS css-builder

WORKDIR /app

COPY package.json bun.lock ./
RUN bun install --frozen-lockfile

COPY app/ app/

RUN bun tailwindcss -i app/static/tailwind.css -o output.css --minify

FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    rsync \
    curl \
    openssl \
    ftpsync

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV PYTHONUNBUFFERED=1

RUN --mount=from=ghcr.io/astral-sh/uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project

COPY app/ app/

RUN --mount=from=ghcr.io/astral-sh/uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen

COPY --from=css-builder /app/output.css app/static/output.css

RUN mkdir -p /var/lock/mirrord

EXPOSE 8080

ENV MIRRORD_CONFIG=config.yaml

ARG GIT_COMMIT=unknown
ARG APP_VERSION=dev
ENV MIRRORD_GIT_COMMIT=${GIT_COMMIT}
ENV MIRRORD_VERSION=${APP_VERSION}

ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "app.main"]