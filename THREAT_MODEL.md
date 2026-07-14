# Threat Model

> ProofGate is a rigorous local reference implementation and portfolio
> demonstration. It is not presented as production-ready infrastructure.

## Protected assets

- production user records (`operations/working.db`, `environment="production"` rows)
- test user records (same database, `environment="test"` rows)
- feature-flag state (`operations/feature_flags_working.json`)
- recovery snapshots (`operations/snapshots/`)
- proof metadata (server-side snapshot metadata keyed by `snapshot_id`)
- workflow-budget state (in-process, keyed by `workflow_id`)
- audit integrity (`artifacts/audit.jsonl`)
- MCP protocol integrity (the stdio JSON-RPC channel)
- deterministic policy behavior (the four rule constants and their trigger logic)

## Threats and current mitigations

### Overbroad selector
An agent (or a human) submits a mutation-selecting argument set that is
broader than intended (e.g. omitting `environment`).
- **Mitigation:** deterministic impact preview always computes the real
  count first; `RULE_INTENT_BOUNDARY` fires when the request's actual
  scope disagrees with the extracted intent; `RULE_WORKFLOW_BUDGET`
  fires when the projected mutation would exceed the workflow's row
  budget.

### Missing environment filter
The specific, demonstrated failure mode: an instruction implies
test-only scope, but the submitted call omits `environment`, and
production rows are silently included.
- **Mitigation:** impact preview is conservative and includes
  production rows whenever they match; `RULE_INTENT_BOUNDARY` catches
  the disagreement; `RULE_WORKFLOW_BUDGET` independently catches the
  scale.

### Fabricated or tampered recovery proof
A caller submits a `RollbackProof` with an invented or altered
`snapshot_id`, `selector_hash`, or `max_affected_rows`.
- **Mitigation:** `proofgate/proofs.py::validate_rollback_proof` looks up
  the snapshot by ID in server-side metadata and cross-checks every
  field against that stored record, never against the caller's copy.
  An unknown ID, a missing artifact file, or any field mismatch fails
  validation. `RULE_RECOVERY_PROOF` blocks a hard-delete action with no
  valid proof.

### Replayed or mismatched proof
A valid proof from one action is submitted against a different action.
- **Mitigation:** the canonical selector hash
  (`proofgate/selector.py::compute_selector_hash`) binds a proof to the
  exact selector arguments it was created for; server-side association
  checks in `validate_rollback_proof` compare the caller's selector hash
  against the snapshot's own recorded selector hash. There is **no**
  single-use token, expiry, or full proof-lifecycle/revocation system --
  a proof remains valid for repeated use against the same selector for
  as long as its snapshot artifact exists. This is not claimed to be a
  complete lifecycle system.

### Repeated small actions
Many small, individually-under-threshold mutations accumulate to a large
total.
- **Mitigation:** `RULE_WORKFLOW_BUDGET` accounts cumulative
  `rows_mutated` per `workflow_id` across every call in that workflow,
  not just the current one.

### Unknown tool
A request names a `tool_name` that is not registered.
- **Mitigation:** `guarded_execute` fails closed via
  `RULE_UNKNOWN_IMPACT` -- no preview, no mutation, no snapshot, no
  budget consumption for an unregistered tool.

### Malformed arguments
A request supplies the wrong type, an out-of-range value, or an
unexpected extra field.
- **Mitigation:** typed MCP request models
  (`GuardedToolRequest`/`FeatureFlagToolRequest`) with
  `extra="forbid"` reject unknown fields and validate types/ranges
  before anything reaches the guarded pipeline.

### Direct Operations bypass
Code calls `operations.actions.delete_users` (or the feature-flag
equivalent) directly, skipping policy entirely.
- **Mitigation:** the public MCP tools route exclusively through
  `guarded_execute(...)`; Operations functions accept no
  `ActionContext`, no proof, and no policy/verdict/audit metadata, so
  there is nothing for a bypassing caller to even supply that would make
  the call look governed. **This is a Python module-boundary
  convention, not a cryptographic or process-level security boundary.**
  Anything running in the same Python process with import access can
  call an Operations function directly; nothing prevents that at the
  language level.

### Post-execution mismatch
The real mutation's outcome differs from what was predicted.
- **Mitigation:** postcondition verification always runs after an
  ALLOWed execution; for a rollback-eligible action (hard-delete, valid
  proof, mismatch), automatic restoration runs and is independently
  verified via digest/count comparison before `ROLLED_BACK` is ever
  reported; any failure at any step reports `MANUAL_REVIEW_REQUIRED`
  instead.

### Demo hook leakage
The deterministic mismatch-injection mechanism
(`DEMO_SIMULATE_POSTCONDITION_MISMATCH`) accidentally activates or
leaks into a real scenario.
- **Mitigation:** off by default; requires an explicit environment
  variable; hardcoded to `environment='test'` rows only, never
  production; the reliable-demo path and the offline test suite never
  set this variable; regression tests confirm its absence has zero
  effect.

## Recovery limitations, precisely

Three distinct concepts are easy to blur together. This repository keeps
them separate:

- **Recovery proof** -- evidence validated *before* execution showing
  that a registered recovery artifact exists and is bound (by selector
  hash) to the exact action about to run.
- **Restoration** -- the real operation that attempts to recover mutated
  state after a genuine postcondition mismatch.
- **Independent verification** -- a separate check, run after
  restoration, confirming the restored state matches the trusted
  pre-execution state (via digest and row-count comparison against the
  snapshot artifact itself).

**Current sandbox limitation:** the hard-delete snapshot artifact is a
complete copy of the SQLite database at snapshot time, but restoration
performs a transactional synchronization of the snapshot's `test`-
environment rows into the live working database -- not a whole-file
replacement. This is safe under this demo's single-process assumption
(no concurrent writer can have changed unrelated rows between snapshot
creation and restoration), but it is **not** safe to assume under an
uncontrolled, concurrent production system, where a second writer could
have legitimately modified rows in the interim that a naive restore
could clobber or that this sandbox's simplifying assumption doesn't
account for. Production evolution of this mechanism would require
operation-scoped recovery bundles, exact changed-row capture,
version-aware conflict detection, idempotency keys, coordination with
concurrent writers, encrypted snapshot storage, retention/lifecycle
management, a separately authorized recovery operation, and durable
recovery audit events -- see `docs/production-evolution.md`. This
repository does **not** claim zero data-loss guarantees, universal
rollback safety, concurrent rollback correctness, or production-grade
restoration.

## Explicitly unsupported or unguaranteed

- cross-process concurrency (workflow budget and postcondition state
  live in one process's memory; a second process has its own,
  independent state -- see the recovery-limitations note above)
- simultaneous MCP and Streamlit writers against the same database
- distributed workflow budgets
- distributed locks
- crash recovery after `SIGKILL`, power loss, or abrupt process
  termination
- multi-tenant isolation
- authentication and authorization
- network perimeter security
- production secrets management
- real feature-flag/notification provider propagation behavior
- eventual consistency handling
- external side effects that cannot honestly be recovered (e.g. an
  email already sent)
- an external audit sink
- cryptographic audit-log chaining (the local audit file is
  append-oriented, but it is **not** tamper-evident, hash-chained, or
  externally immutable)
- production deployment of any kind

> ProofGate is a rigorous local reference implementation and portfolio
> demonstration. It is not presented as production-ready infrastructure.
