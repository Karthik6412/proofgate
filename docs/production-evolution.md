# Production Evolution

This document describes plausible future phases beyond the current
reference implementation. **None of the phases below are implemented.**
They exist to show the reader that the current scope was a deliberate
choice, not a ceiling on the design, without pretending any of this work
has actually been done.

## Phase 1 — Current reference implementation

What exists today:

- a local MCP gateway (stdio transport, single process)
- a deterministic policy engine (four rules, no model in the verdict path)
- SQLite and JSON backends for two genuinely different resource types
- local, in-process proof metadata
- local, in-process workflow budget
- a local, append-only audit log
- single-process execution (no concurrency control)
- a deterministic fallback mode that requires no live credentials

## Phase 2 — Authenticated service boundary

- authenticated callers (replacing the current unauthenticated stdio
  gateway)
- durable agent identity
- durable tenant identity
- authorization scopes (which agent/tenant may call which tool at all --
  a layer above, not a replacement for, blast-radius policy)
- a durable relational store (replacing in-process budget/proof state)
- centralized budget state across processes and restarts
- a secrets manager (replacing a local `.env` file)
- structured telemetry
- a remote, append-only audit sink

## Phase 3 — Concurrent execution safety

- idempotency keys for retried requests
- optimistic concurrency / version checks on mutated rows
- operation-level locks
- durable workflow state (surviving process restart)
- transaction coordination across concurrent writers
- crash recovery after abrupt termination
- retry policies with backoff

## Phase 4 — Production recovery

- operation-scoped recovery bundles (not a whole-database copy)
- encrypted artifact storage
- artifact retention and lifecycle management
- single-use or lifecycle-aware proof (closing the current "a proof
  remains valid for repeated use" limitation)
- conflict-aware restoration under concurrent writers
- a separate recovery-authorization step (recoverability and
  authorization remain distinct concepts even here)
- durable recovery audit records
- reconciliation against the real source of truth after restoration

## Phase 5 — External providers

- provider-specific preview APIs (e.g. a real feature-flag provider's
  own dry-run/preview endpoint, replacing the local deterministic
  adapter)
- provider dry-run support
- remote idempotency guarantees
- propagation monitoring (confirming a change actually reached
  downstream consumers)
- eventual-consistency handling
- compensating actions where a real provider's mutation cannot be
  cleanly reversed
- provider-side reconciliation

---

No deployment infrastructure (Docker, Kubernetes, Terraform, CI/CD, or
cloud resources) is added in any phase described here -- that is a
separate, later concern this document deliberately does not address.
