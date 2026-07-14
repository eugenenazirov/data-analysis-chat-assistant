# Implementation Milestones

The prototype milestones below are complete and intentionally correspond to
separate reviewable commits.

1. Reconcile the reproducible runtime and configuration baseline.
2. Upgrade and lock the PydanticAI 2.9 dependency surface.
3. Establish domain, application, infrastructure, presentation, and composition
   boundaries with import-direction tests.
4. Replace ad hoc configuration with nested Pydantic Settings and packaged,
   versioned prompts.
5. Route conversational analysis through application use cases and ports while
   preserving the CLI contract.
6. Introduce model-selected retrieval and SQL tools, discriminated structured
   output, evidence validation, complete bounded history, and usage budgets.
7. Add dynamically enabled automatic chart generation with bounded local
   execution and production isolation guidance.
8. Separate guardrail and answer-quality evaluations, their dependencies, data,
   entrypoint, and container image from the runtime surface.
9. Reconcile reviewer documentation, acceptance commands, deployment behavior,
   and requirements mapping with the implemented architecture.
10. Establish versioned fixture provenance, dataset validation, overlap policy,
    and smoke/development/release partitions.
11. Strengthen deterministic scoring with exact result contracts, retrieval
    ranking and downstream-harm diagnostics, expected-behavior checks, and
    operational budgets.
12. Expand held-out analytical, multi-turn, retrieval, adversarial, and minimized
    regression coverage to 67 replay cases.
13. Add repeated credentialed runs, reliability intervals, flake detection,
    trajectory-level telemetry, and separately accounted reference queries.
14. Add calibrated two-reviewer scoring, separate blind A/B and pointwise
    packets, accepted-baseline comparison, and explicit release blockers.
15. Add tiered offline, canary, release-candidate, immutable-artifact, and
    separate approval workflows.
16. Run the complete specialist cleanup and regression matrix, including runtime
    and evaluation container-boundary verification.

## Production Follow-On

The production HLD deliberately remains future work:

- OIDC-authenticated HTTP and administration APIs;
- PostgreSQL conversations, ownership, reports, confirmations, audit, and
  transactional outbox;
- an isolated chart worker with strong compute and network containment;
- analyst-reviewed Golden Knowledge promotion and atomic index rollback;
- persona publishing, immutable audit exports, OpenTelemetry operations, HA,
  backups, and recovery testing.

See `docs/architecture.md` for those contracts and `docs/qa.md` for the current
prototype release gates.
