# Agent Flight Deck

An interactive guide to the real request and data flow of
`data-analysis-chat-assistant`. It runs independently of the Python runtime and does
not require Google credentials, BigQuery, or Qdrant.

## Run locally

```bash
cd agent-flow-explorer
npm install
npm run dev
```

Vite prints the local URL, which defaults to `http://127.0.0.1:5173`.

Production verification:

```bash
npm run lint
npm run build
npm run preview
```

## What it covers

- **Request Flow** — eight scenarios with playback, dynamic node availability, a
  step inspector, and a synchronized event stream.
- **Architecture** — interactive Domain / Application / Infrastructure /
  Presentation boundaries and the composition root.
- **Guardrails** — independent safety gates and exact budgets from
  `config/agent.yaml`.
- **Telemetry** — a trace-oriented walkthrough of `logs/agent-runs.jsonl`.

Every technical label and code pointer is grounded in the current repository. This
is an explanatory static simulator: it does not call a real LLM or warehouse, and
it is not a second product UI.

## Design reference

The visual reference used to build the interface:
`design/agent-flight-deck-concept.png`.
