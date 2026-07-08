# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/app" \
    --uid 10001 \
    appuser

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip && \
    python -m pip install -r requirements.txt

COPY . .

RUN mkdir -p /app/logs && chown -R appuser:appuser /app

USER appuser

ENTRYPOINT ["python", "-m", "retail_agent"]
CMD ["--help"]
