# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.10.8 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/app" \
    --uid 10001 \
    appuser

COPY --from=builder /app/.venv /app/.venv
COPY retail_agent ./retail_agent
COPY config ./config
COPY data ./data

RUN install -d -o appuser -g appuser /app/logs

USER appuser

ENTRYPOINT ["python", "-m", "retail_agent"]
CMD ["--help"]
