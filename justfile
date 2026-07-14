set unstable := true
set positional-arguments := true
set shell := ["bash", "-euo", "pipefail", "-c"]

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

# Run all credential-free evaluation suites.
eval: dataset guardrails quality quality-holdout quality-multi-turn quality-retrieval quality-adversarial

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
    docker run --rm retail-agent:evaluation guardrails
    docker run --rm retail-agent:evaluation quality --mode replay

# Prepare the environment and run the full local and container acceptance matrix.
review: setup _local-check container-check

# Start the local Qdrant service.
qdrant-up:
    docker compose up -d qdrant

# Stop the local Qdrant service without deleting its data volume.
qdrant-down:
    docker compose stop qdrant

# Recreate the approved Golden Knowledge index with the application container.
index-golden:
    docker compose run --rm app index-golden --recreate

# Ask one question through the application container.
ask question user="manager_a":
    docker compose run --rm app ask "$1" --user "$2"

# Start an interactive conversation through the application container.
chat user="manager_a":
    docker compose run --rm app chat --user "$1"

# Run the live BigQuery smoke query through the application container.
bq-smoke:
    docker compose run --rm --no-deps app bq-smoke
