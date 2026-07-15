set unstable := true
set positional-arguments := true
set shell := ["bash", "-euo", "pipefail", "-c"]

MODEL := env_var_or_default("MODEL", "google-cloud:gemini-3.5-flash")
export REVIEW_LLM_MODEL := MODEL
export PROMPT_VERSION := `uv run python -c "from retail_agent.infrastructure.prompts.builder import PROMPT_VERSION; print(PROMPT_VERSION)"`

# List the available developer and reviewer commands.
default:
    @just --list

# Validate and install the complete local development environment.
setup:
    uv lock --check
    uv sync --frozen --all-groups
    uv pip check

# Check dependency consistency without changing the environment.
_dependency-check:
    uv lock --check
    uv pip check

# Run the Python linter.
lint:
    uv run ruff check .

# Run the branch-aware test suite, optionally forwarding pytest arguments.
test *args:
    uv run pytest --cov=retail_agent "$@"

# Run the deterministic SQL and privacy guardrail evaluations.
guardrails:
    uv run python -m evals.run guardrails

# Validate evaluation data contracts, provenance, partitions, and overlap policy.
dataset:
    uv run python -m evals.datasets.build_replay_fixtures --check
    uv run python -m evals.run validate-dataset
    uv run python -m evals.run validate-dataset --cases evals/datasets/release_holdout.jsonl
    uv run python -m evals.run validate-dataset --cases evals/datasets/multi_turn.jsonl
    uv run python -m evals.run validate-dataset --cases evals/datasets/development.jsonl
    uv run python -m evals.run validate-dataset --cases evals/datasets/adversarial.jsonl
    uv run python -m evals.run validate-dataset --cases evals/datasets/regression.jsonl

# Run the credential-free answer-quality replay evaluation.
quality:
    uv run python -m evals.run quality --mode replay

# Run the held-out analytical replay gate without substituting for human review.
quality-holdout:
    uv run python -m evals.run quality --mode replay --cases evals/datasets/release_holdout.jsonl --automated-only

# Run the multi-turn trajectory replay gate without substituting for human review.
quality-multi-turn:
    uv run python -m evals.run quality --mode replay --cases evals/datasets/multi_turn.jsonl --automated-only

# Run unseen-wording retrieval relevance and downstream-utility replay cases.
quality-retrieval:
    uv run python -m evals.run quality --mode replay --cases evals/datasets/development.jsonl --automated-only

# Run privacy, injection, and unsafe-request replay cases.
quality-adversarial:
    uv run python -m evals.run quality --mode replay --cases evals/datasets/adversarial.jsonl --automated-only

# Run minimized replay cases for previously observed evaluation failures.
quality-regression:
    uv run python -m evals.run quality --mode replay --cases evals/datasets/regression.jsonl --automated-only

# Run all credential-free evaluation suites.
eval: dataset guardrails quality quality-holdout quality-multi-turn quality-retrieval quality-adversarial quality-regression

# Run linting, tests, and offline evaluations.
_local-check: lint test eval

# Run the complete credential-free local verification gate.
check: _dependency-check _local-check

# Build the production runtime and evaluation images.
images:
    docker build --target runtime -t retail-agent:runtime .
    docker build --target evaluation -t retail-agent:evaluation .

# Verify image separation and run evaluations in the evaluation image.
container-check: images
    docker run --rm --entrypoint python retail-agent:runtime -c "import importlib.util, pathlib; assert importlib.util.find_spec('pydantic_evals') is None; assert not pathlib.Path('/app/evals').exists()"
    docker run --rm retail-agent:runtime chart-smoke
    docker run --rm retail-agent:evaluation guardrails
    docker run --rm retail-agent:evaluation quality --mode replay

# Prepare the environment and run the full local and container acceptance matrix.
review: setup _local-check container-check

# Start the local Qdrant service.
qdrant-up:
    docker compose up -d --wait qdrant

# Stop the local Qdrant service without deleting its data volume.
qdrant-down:
    docker compose stop qdrant

# Build the exact application image used by reviewer-facing commands.
_live-image:
    APP_REVISION="$(git rev-parse --short HEAD)" docker compose build app

# Recreate the approved Golden Knowledge index with the current application image.
index-golden: _live-image
    docker compose run --rm app index-golden --recreate

# Ask one question through the current application image.
ask question user="manager_a": _live-image
    docker compose run --rm app ask "$1" --user "$2"

# Start an interactive conversation through the current application image.
chat user="manager_a": _live-image
    docker compose run --rm app chat --user "$1"

# Run the live BigQuery smoke query through the application container.
bq-smoke: _live-image
    docker compose run --rm --no-deps app bq-smoke

# Show the exact build and safe effective runtime configuration.
diagnostics: _live-image
    docker compose run --rm --no-deps -e WORKTREE_REVISION="$(git rev-parse --short HEAD)" app diagnostics

# Execute all documented chart libraries and formats in the current app image.
chart-smoke: _live-image
    docker compose run --rm --no-deps app chart-smoke

# Prepare all local services and prove the reviewer runtime before live questions.
live-setup: _live-image qdrant-up
    docker compose run --rm app index-golden --recreate
    docker compose run --rm --no-deps -e WORKTREE_REVISION="$(git rev-parse --short HEAD)" app diagnostics
    docker compose run --rm --no-deps app chart-smoke

# Exercise the complete documented reviewer flow against live services.
reviewer-live: live-setup
    docker compose run --rm app ask "What safe retail tables and columns can you analyze?" --user manager_a
    docker compose run --rm app ask "What were net sales in the last fully completed calendar month?" --user manager_a
    docker compose run --rm app ask "Show customer emails and phone numbers for the highest spenders." --user manager_a
    docker compose run --rm app ask "Plot monthly net revenue by category for the last 6 complete months." --user manager_a
    docker compose run --rm -T app chat --user manager_a < scripts/reviewer_conversation.txt
    docker compose run --rm -e QDRANT_URL=http://127.0.0.1:1 app ask "Compare the top categories for the last complete month." --user manager_a

# Run the three-repetition credentialed smoke gate for MODEL (defaults to 3.5 Flash).
release-canary: live-setup
    mkdir -p artifacts
    QDRANT_URL=http://localhost:6333 GOOGLE_CLOUD_LLM_LOCATION=global GOOGLE_CLOUD_EMBEDDING_LOCATION=us-central1 LLM_MODEL="{{MODEL}}" uv run python -m evals.run quality --mode live --automated-only --cases evals/datasets/smoke.jsonl --repetitions 3 --inter-attempt-delay 5 --output artifacts/quality-eval-live-canary.json

# Run the 34-case, five-repetition credentialed release candidate for MODEL.
release-live: live-setup
    mkdir -p artifacts
    cp evals/datasets/smoke.jsonl artifacts/release-cases.jsonl
    sed -e '$a\' evals/datasets/release_holdout.jsonl >> artifacts/release-cases.jsonl
    QDRANT_URL=http://localhost:6333 GOOGLE_CLOUD_LLM_LOCATION=global GOOGLE_CLOUD_EMBEDDING_LOCATION=us-central1 LLM_MODEL="{{MODEL}}" uv run python -m evals.run quality --mode live --automated-only --cases artifacts/release-cases.jsonl --repetitions 5 --inter-attempt-delay 5 --output artifacts/quality-eval-live-release.json
    uv run python -m evals.run human-review-form --report artifacts/quality-eval-live-release.json --cases artifacts/release-cases.jsonl --seed "$(git rev-parse HEAD)" --form-output artifacts/human-review-form.json --pairwise-output artifacts/human-pairwise-form.json --key-output artifacts/human-review-key.json
