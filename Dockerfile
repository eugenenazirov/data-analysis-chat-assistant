# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.10.8 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-group eval

FROM builder AS evaluation-builder

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group eval

FROM python:${PYTHON_VERSION}-slim AS runtime

ARG APP_REVISION=unknown
ARG PROMPT_VERSION=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_REVISION=${APP_REVISION} \
    PROMPT_VERSION=${PROMPT_VERSION} \
    MPLBACKEND=Agg \
    PATH="/app/.venv/bin:$PATH"

LABEL org.opencontainers.image.revision=${APP_REVISION} \
    io.opsfleet.retail-agent.prompt-version=${PROMPT_VERSION}

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

RUN APP_REVISION="${APP_REVISION}" PROMPT_VERSION="${PROMPT_VERSION}" python -c \
    'import json, os; from importlib.metadata import version; import matplotlib, numpy, pandas, seaborn; from retail_agent.infrastructure.prompts.builder import PROMPT_VERSION; expected = os.environ["PROMPT_VERSION"]; assert expected in {"unknown", PROMPT_VERSION}, f"configured prompt {expected} does not match runtime {PROMPT_VERSION}"; open("/app/build-metadata.json", "w", encoding="utf-8").write(json.dumps({"revision": os.environ["APP_REVISION"], "prompt_version": PROMPT_VERSION, "chart_runtime": {name: version(name) for name in ("matplotlib", "numpy", "pandas", "seaborn")}}, sort_keys=True))'

RUN install -d -o appuser -g appuser /app/logs /app/artifacts/charts

USER appuser

ENTRYPOINT ["python", "-m", "retail_agent"]
CMD ["--help"]

FROM runtime AS evaluation

USER root
COPY --from=evaluation-builder /app/.venv /app/.venv
COPY --chown=appuser:appuser evals ./evals
USER appuser

ENTRYPOINT ["python", "-m", "evals.run"]
CMD ["--help"]

FROM runtime AS app
