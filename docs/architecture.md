# Architecture

This document describes the real, implemented system: what each component
does, what it is not allowed to do, and how a request actually flows through
it. Every diagram here matches the current code, not an aspirational design.

## System diagram

```mermaid
flowchart TB
    Client["MCP Client / Streamlit / CLI script"]
    Gateway["MCP Gateway (proofgate/mcp_server.py)"]
    Registry["Registry (proofgate/registry.py)"]
    Execute["Guarded Execute (proofgate/core.py)"]
    Semantic["Semantic Signals (Nebius, optional)"]
    Impact["Deterministic Impact Preview"]
    ProofBudget["Proof Validation / Workflow Budget"]
    Policy["Deterministic Policy (proofgate/policy.py)"]
    Verdict["ALLOW or BLOCK"]
    Ops["Operations Boundary"]
    Users["SQLite users table"]
    Flags["Feature-flag JSON"]
    Post["Postcondition Check"]
    Rollback["Eligible Rollback Resolution"]
    Audit["Audit Event (append-only)"]

    Client --> Gateway --> Registry --> Execute
    Execute --> Semantic
    Execute --> Impact
    Execute --> ProofBudget
    Semantic --> Policy
    Impact --> Policy
    ProofBudget --> Policy
    Policy --> Verdict
    Verdict -->|ALLOW| Ops
    Ops --> Users
    Ops --> Flags
    Ops --> Post
    Post --> Rollback
    Verdict --> Audit
    Rollback --> Audit
```

### Component responsibilities

**Nebius (semantic extraction only)**
- May identify structured intent and risk signals from the free-text
  instruction (`environment`, `inactivity_days`, mismatch/scope-class
  features).
- Does **not** calculate authoritative impact.
- Does **not** issue the verdict.
- Live-preferred with a deterministic regex fallback on any failure,
  malformed response, or missing configuration.

**CRAFT (read-only evidence, optional)**
- A separate, explicitly user-triggered enterprise-schema/cohort-evidence
  source, rendered in Streamlit under an honest "CRAFT enterprise
  evidence" / "Previously retrieved CRAFT evidence" label.
- Does **not** authorize actions.
- Does **not** issue the verdict.
- Never called from the MCP gateway or from any test.

**Deterministic code (the only authority)**
- Calculates impact (`operations/actions.py`, `operations/feature_flags.py`).
- Validates proof (`proofgate/proofs.py`).
- Checks workflow budget (`proofgate/budgets.py`).
- Evaluates policy (`proofgate/policy.py`) -- the sole ALLOW/BLOCK authority.
- Executes Operations mutations, only after a genuine ALLOW.
- Verifies postconditions (`proofgate/postcondition.py`).
- Decides rollback eligibility and resolves the final postcondition status
  (`proofgate/rollback.py`).
- Writes exactly one audit event per governed call (`proofgate/audit.py`).

### Postcondition accuracy note

Two genuinely different mechanisms exist, and this document (and the rest
of the repository) is careful never to conflate them:

- **Normal postcondition verification** (`proofgate/postcondition.py::
  verify_postcondition`) compares the deterministic preview's predicted
  count against the mutation's own honestly-reported actual count. It is
  a **count comparison**, not a digest comparison, and it never re-queries
  the database.
- **Rollback's independent restoration verification**
  (`proofgate/rollback.py::resolve_final_postcondition`) is a separate,
  additional mechanism that only runs when normal verification reports
  `MISMATCH` on a rollback-eligible action. It recomputes a row-content
  digest and row counts directly from the snapshot artifact and compares
  them to the post-restoration database state -- this is where digest
  comparison actually happens.

## Sequence: BLOCK

```mermaid
sequenceDiagram
    participant Client
    participant MCP as MCP Gateway
    participant Registry
    participant Semantic as Semantic Extraction
    participant Impact as Impact Preview
    participant Policy as Deterministic Policy
    participant Audit

    Client->>MCP: call_tool(delete_users, broad args)
    MCP->>Registry: look up tool spec
    MCP->>Semantic: extract intent (live-preferred, deterministic fallback)
    MCP->>Impact: preview_delete_users(...)
    Impact-->>MCP: ImpactEnvelope (10,073 / 9,981 production / 92 test)
    MCP->>Policy: evaluate_policy(intent, impact, proof, budget)
    Policy-->>MCP: BLOCK, {RULE_INTENT_BOUNDARY, RULE_RECOVERY_PROOF, RULE_WORKFLOW_BUDGET}
    Note over MCP: Operations is never called on BLOCK
    MCP->>Audit: append one AuditEvent
    MCP-->>Client: verdict=BLOCK, executed=false
```

## Sequence: ALLOW

```mermaid
sequenceDiagram
    participant Client
    participant MCP as MCP Gateway
    participant Impact as Impact Preview
    participant ProofBudget as Proof / Budget Checks
    participant Policy as Deterministic Policy
    participant Ops as Operations
    participant Post as Postcondition
    participant Audit

    Client->>MCP: call_tool(delete_users, corrected args, valid proof)
    MCP->>Impact: preview_delete_users(...)
    Impact-->>MCP: ImpactEnvelope (92 test / 0 production)
    MCP->>ProofBudget: validate_rollback_proof(...), check budget
    ProofBudget-->>MCP: proof VALID, budget OK
    MCP->>Policy: evaluate_policy(...)
    Policy-->>MCP: ALLOW, no triggered rules
    MCP->>Ops: delete_users(inactive_days=90, environment="test")
    Ops-->>MCP: MutationResult(affected=92, production=0, test=92)
    MCP->>Post: verify_postcondition(impact, mutation_result)
    Post-->>MCP: VERIFIED (count comparison: predicted 92 == actual 92)
    MCP->>Audit: append one AuditEvent
    MCP-->>Client: verdict=ALLOW, executed=true, postcondition=VERIFIED
```

## Sequence: one-shot repair

```mermaid
sequenceDiagram
    participant Model as Repair-proposal model call
    participant Script as Trusted script (scripts/agent_loop.py)
    participant MCP as MCP Gateway

    Script->>MCP: initial proposal (delete_users, broad)
    MCP-->>Script: BLOCK + sanitized suggested_repairs
    Note over Script: exactly one repair-model call, given only the<br/>instruction, original proposal, verdict, triggered rules,<br/>and suggested_repairs -- never proof, audit, or budget internals
    Script->>Model: repair prompt
    Model-->>Script: revised proposal (e.g. deactivate_users, corrected environment)
    Script->>MCP: second submission, same workflow ID, rollback_proof=null
    MCP-->>Script: final result (ALLOW or BLOCK)
```

Invariants: at most one repair-model call per workflow; at most two MCP
submissions per workflow; the same workflow ID is reused for both; no
proof is fabricated or attached by the repair path; suggested repairs are
never authoritative -- only the deterministic policy's own re-evaluation
of the repaired request decides the second verdict.

## Sequence: automatic rollback

```mermaid
sequenceDiagram
    participant Client
    participant MCP as MCP Gateway
    participant Ops as Operations (delete_users)
    participant Post as Postcondition
    participant Rollback as Rollback Resolution
    participant Restore as Trusted Restoration
    participant Audit

    Client->>MCP: call_tool(delete_users, valid snapshot proof)
    MCP->>Ops: delete_users(...)
    Note over Ops: DEMO_SIMULATE_POSTCONDITION_MISMATCH=true (off by default,<br/>local-sandbox only): one real extra deletion, honestly counted
    Ops-->>MCP: MutationResult(affected=93, not the predicted 92)
    MCP->>Post: verify_postcondition(impact, mutation_result)
    Post-->>MCP: MISMATCH (count comparison: 92 predicted != 93 actual)
    MCP->>Rollback: resolve_final_postcondition(...)
    Rollback->>Restore: restore_snapshot(snapshot_id, expected_selector)
    Restore-->>Rollback: RESTORED (test rows synchronized from snapshot)
    Rollback->>Rollback: independent digest + row-count verification
    Rollback-->>MCP: ROLLED_BACK
    MCP->>Audit: append the one enforcement AuditEvent (postcondition=ROLLED_BACK)
    MCP-->>Client: verdict=ALLOW, executed=true, postcondition=ROLLED_BACK
```

Facts worth stating plainly:
- The mismatch mechanism is deterministic, local, off by default, and
  exists only to demonstrate genuine mismatch detection -- it performs a
  real extra database mutation and never fabricates `PostconditionResult`.
- Workflow budget is charged once, from the real total mutation
  (`93/100`, not `92/100`) -- rollback does not refund it.
- Failed restoration or failed independent verification produces
  `MANUAL_REVIEW_REQUIRED`, never a false `ROLLED_BACK`.
- Exactly one enforcement audit event represents the whole call; rollback
  never appends a second, fake enforcement decision.
