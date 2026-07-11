# ProofGate Repository Instructions

## Mission

Build the hackathon MVP described in `docs/ProofGate_PRD_FINAL_v4.md`.

ProofGate measures the blast radius of consequential AI-agent actions before execution.

Do not broaden the product beyond the PRD.

---

## Python Environment

Before running Python-related commands, use the repository virtual environment.

Prefer:

```bash
source .venv/bin/activate
```

If activation persistence is uncertain across separate shell invocations, use the
explicit executables instead:

- .venv/bin/python
- .venv/bin/pytest
- .venv/bin/pip

Never install packages globally.

---

## Non-Negotiable Demo

The user requests:

> Clean up inactive test accounts that have not logged in for 90 days.

The simulated bad proposal is:

```python
delete_users(inactive_days=90)
```

It omits:

```python
environment="test"
```

The deterministic seed must produce:

- 9,981 inactive production users
- 92 inactive test users
- 10,073 total affected by the broad call

The protected flow must show:

1. Broad delete is `BLOCKED`.
2. No mutation occurs after `BLOCK`.
3. Only actually triggered policy rules are displayed.
4. Agent adds `environment="test"`.
5. Agent creates a selector-bound snapshot proof.
6. The same public `delete_users` tool is retried.
7. Corrected action is `ALLOWED`.
8. Predicted count = 92.
9. Actual count = 92.
10. Production affected = 0.
11. Postcondition status = `VERIFIED`.
12. Workflow budget = 92/100.

---

## Frozen Architectural Boundary

The public ProofGate boundary accepts policy context and proof:

```python
guarded_delete_users(
    action_context,
    inactive_days,
    environment,
    rollback_proof,
)
```

The internal Operations mutation stays dumb:

```python
operations.delete_users(
    inactive_days,
    environment,
)
```

`rollback_proof` belongs to ProofGate, never to the underlying mutation function.

Protected mode may call `operations.delete_users` only after ProofGate returns `ALLOW`.

Unprotected mode intentionally calls it directly.

Do not change this contract without explicit user approval.

---

## Architectural Invariants

- CRAFT is read-only enterprise intelligence.
- CRAFT does not perform deletion.
- SQLite shadow CRM is authoritative for exact mutation impact.
- Nebius extracts intent and structured risk features.
- Nebius must never return `ALLOW` or `BLOCK`.
- Deterministic Python policy makes every verdict.
- Rollback proof is recoverability, not authorization.
- Valid proof must never override an intent mismatch.
- Unknown impact for a consequential action fails closed.
- Workflow mutation budget is 100 rows.
- Production deletion budget is zero.
- Risk factors and triggered policy rules are separate concepts.
- The UI renders only rules that actually fired.

---

## Selector Hash Contract

All selector hashes must use one shared canonicalization function.

Canonical JSON:

```python
json.dumps(
    arguments,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=True,
)
```

Hash the resulting UTF-8 bytes using SHA-256.

The selector hash must include only mutation-selecting arguments.

Include:

- `inactive_days`
- `environment`

Exclude:

- `rollback_proof`
- `action_context`
- workflow metadata
- policy metadata
- risk metadata
- timestamps
- audit fields

For the corrected demo action, the selector is logically equivalent to:

```json
{
  "environment": "test",
  "inactive_days": 90
}
```

The same canonicalization function must be used by:

- impact preview
- snapshot creation
- proof validation
- guarded execution

Do not duplicate selector-hash logic across modules.

---

## Nebius Output Rules

Nebius is used for:

1. Intent extraction.
2. Structured risk-feature extraction.

Nebius responses must:

- use strict JSON
- match Pydantic models
- never invent counts
- use `ImpactEnvelope` counts as authoritative
- return semantic features only
- never return `ALLOW` or `BLOCK`
- have a deterministic regex fallback

Python policy code makes the final verdict.

---

## CRAFT Claim Discipline

Display CRAFT evidence separately from operational impact.

Use these labels:

- `CRAFT enterprise evidence`
- `Operations impact preflight`

CRAFT provides enterprise context and read-only analytical evidence.

The Operations preflight calculates the authoritative mutation blast radius.

Never imply cached evidence is live.

If cached evidence is used, label it:

- `Previously retrieved CRAFT evidence`

---

## Demo Interaction Discipline

The complete demo should require no more than:

1. Run Unprotected.
2. Reset and Run Protected.
3. Optional details expansion.

Snapshot creation is real but automatic.

The main UI may jump directly to a populated `PROOF VALID` panel.

Do not require separate user clicks for:

- repair
- snapshot creation
- proof validation
- corrected retry
- postcondition verification

---

## Safety

- All mutations target only `operations/working.db`.
- Never mutate `operations/pristine.db`.
- Never run destructive commands outside the repository.
- Do not print API keys, OAuth tokens, or complete environment variables.
- Do not commit `.env`, token caches, credentials, or generated secrets.
- Ask before deleting files.
- Ask before changing the frozen architecture.
- Ask before adding dependencies not listed as approved.

---

## Approved Dependencies

The following dependencies are pre-approved:

- `pydantic`
- `openai`
- `mcp`
- `FastMCP`
- `streamlit`
- `pytest`
- `rich`
- `python-dotenv`

Prefer the Python standard library where practical.

Ask before adding anything else.

---

## Build Order

1. Deterministic database schema, seed, and reset.
2. Unprotected destructive flow.
3. `ImpactEnvelope` and preview.
4. Deterministic `BLOCK`.
5. Structured repair.
6. Snapshot proof.
7. Corrected same-tool `ALLOW`.
8. Postcondition verification.
9. Workflow budget.
10. JSONL audit.
11. Nebius integration.
12. CRAFT integration.
13. Streamlit UI.
14. Thin FastMCP adapter.

Do not build UI polish before the complete terminal flow works.

---

## Vertical-Slice Discipline

Implement one slice at a time.

After each slice:

1. Run the smallest relevant test.
2. Report exact commands.
3. Report exact results.
4. List files changed.
5. List unverified assumptions.
6. Stop before starting the next slice unless asked.

Do not silently continue into the next slice.

---

## Policy Rules

The deterministic policy supports these hard-block rules:

### `RULE_INTENT_BOUNDARY`

Trigger when:

```text
intent requires environment=test
AND preview includes production rows
```

### `RULE_RECOVERY_PROOF`

Trigger when:

```text
action is irreversible
AND no valid action-bound rollback proof exists
```

### `RULE_UNKNOWN_IMPACT`

Trigger when:

```text
impact preview failed or is unknown
AND the tool is consequential
```

Unknown impact must fail closed.

### `RULE_WORKFLOW_BUDGET`

Trigger when:

```text
rows_mutated_so_far + proposed_affected_rows > 100
```

Risk score is explanatory only.

Triggered deterministic rules decide the verdict.

---

## UI Correctness

Risk factors explain severity.

Triggered rules explain enforcement.

Do not render a canned list of reasons.

For the broad bad action, the triggered rules must be:

- `RULE_INTENT_BOUNDARY`
- `RULE_RECOVERY_PROOF`
- `RULE_WORKFLOW_BUDGET`

The broad bad action must not trigger:

- `RULE_UNKNOWN_IMPACT`

because the operational preview succeeded.

The UI must display only rules returned by the policy engine.

---

## Proof Correctness

A rollback proof is valid only when:

1. The snapshot exists.
2. The resource matches `users`.
3. The selector hash matches the exact mutation arguments.
4. The estimated affected count is less than or equal to `max_affected_rows`.

A valid proof must never override:

- intent mismatch
- forbidden production impact
- unknown impact
- workflow budget violations

Proof is recoverability, not authorization.

---

## Operations Boundary Correctness

The internal Operations module may:

- preview affected rows
- create snapshots
- delete rows
- reset the working database
- return mutation results

The Operations module must not:

- evaluate user intent
- calculate risk scores
- return `ALLOW` or `BLOCK`
- inspect `ActionContext`
- inspect rollback proof
- enforce workflow budget
- write policy decisions
- update UI state

Only the ProofGate boundary may:

- validate intent
- validate rollback proof
- enforce workflow budget
- determine verdict
- call Operations after `ALLOW`
- write enforcement audit events
- run postcondition verification

---

## Quality Gate

Before declaring the core demo complete, verify:

- database resets correctly
- broad preview equals 10,073
- broad protected delete performs zero mutation
- broad call returns:
  - `RULE_INTENT_BOUNDARY`
  - `RULE_RECOVERY_PROOF`
  - `RULE_WORKFLOW_BUDGET`
- broad call does not return:
  - `RULE_UNKNOWN_IMPACT`
- corrected preview equals:
  - 92 test users
  - 0 production users
- snapshot exists
- proof resource matches `users`
- selector hash matches corrected arguments
- corrected public delete affects exactly 92 test users
- production affected equals 0
- postcondition is `VERIFIED`
- workflow budget becomes 92/100
- JSONL audit contains both `BLOCK` and `ALLOW` events
- UI displays risk factors separately from triggered rules
- UI displays only rules that actually fired

---

## Test Discipline

Tests must cover:

- exact seed counts
- reset behavior
- broad preview counts
- direct unprotected deletion
- protected broad-call `BLOCK`
- zero mutation after `BLOCK`
- correct triggered rules
- selector-hash stability
- proof selector mismatch
- proof resource mismatch
- corrected-call `ALLOW`
- exact 92-row mutation
- zero production impact
- postcondition `VERIFIED`
- budget updated to 92/100
- budget rejection above 100
- JSONL `BLOCK` and `ALLOW` records

Unit tests must not depend on:

- live network access
- live CRAFT
- live Nebius
- UI rendering

Use deterministic fallbacks or mocks for unit tests.

Keep separate integration paths for real CRAFT and real Nebius.

---

## Time Discipline

Prefer the smallest functioning implementation.

If a slice exceeds approximately 35–40 minutes without passing its tests:

1. Stop.
2. Report exact blockers.
3. Report what currently works.
4. Recommend the smallest fallback.
5. Do not continue iterating blindly.

Do not add:

- `HOLD` verdict
- human approval workflow
- multiple action scenarios
- hash-chain logging
- enterprise IAM
- deployment infrastructure
- generalized policy DSL
- additional databases
- multiple UI pages
- background workers
- queues

unless the complete demo passes three consecutive times.

---

## Completion Standard

Never claim a slice or project is complete without stating:

- exact files changed
- exact commands run
- exact tests passed
- exact tests not run
- live integrations verified
- live integrations not verified
- known risks
- next recommended action

A small complete demo is better than a broad unstable prototype.