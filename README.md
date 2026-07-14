# ProofGate

ProofGate is a deterministic runtime enforcement layer between AI agents
and consequential tools. It previews blast radius, applies
machine-verifiable policy, requires recovery evidence where appropriate,
and records what actually happened before and after execution.

## The failure this exists to stop

```text
Instruction:
Clean up inactive test accounts that have not logged in for 90 days.

Proposed call:
delete_users(inactive_days=90, environment=None)
```

Deterministic preview against the seeded data:

```text
10,073 affected
9,981 production
92 test
```

**Permission to use a tool is not permission for every invocation of that
tool.** The agent was authorized to call `delete_users`. That specific
call, with those specific arguments, would have deleted 9,981 real
production records because one filter never made it into the call.
ProofGate measures that gap before execution, not after.

## What ProofGate does

```text
Instruction and proposed call
  -> MCP gateway
  -> registry
  -> semantic intent extraction
  -> deterministic impact preview
  -> deterministic policy
  -> ALLOW or BLOCK
  -> Operations execution
  -> postcondition verification
  -> rollback when eligible
  -> audit
```

See `docs/architecture.md` for full system and sequence diagrams.

## Why this differs from prompt-based safety

- an LLM may extract structured semantic signals (intent, scope, risk
  features) from the instruction
- deterministic adapter code -- never a model -- calculates the actual
  impact against the real system being mutated
- deterministic policy rules are the **sole** verdict authority; nothing
  else in the system can say ALLOW or BLOCK
- proof validation is server-side: a caller's proof is cross-checked
  against stored metadata, never trusted as given
- workflow budgets are deterministic, cumulative, and enforced in code
- Operations functions (the actual mutation code) do not evaluate policy,
  proof, or budget -- they cannot decide whether their own call was
  authorized
- the agent cannot authorize itself: proposing a tool call and having
  that call ALLOWed are evaluated by completely separate code
- repair guidance is advisory only and never affects a verdict

The same tool call reaches a different verdict purely based on real,
measured impact and proof -- never based on which agent asked, how it
phrased the request, or how confident it sounded:

```text
delete_users(inactive_days=90, environment=None)
  -> BLOCK

delete_users(inactive_days=90, environment="test")
  with a valid, server-validated recovery proof
  -> ALLOW
```

ProofGate does not guarantee AI safety in general, does not eliminate
agent risk, and is not production-ready infrastructure. See
`THREAT_MODEL.md` for exactly what is and isn't covered.

## What's actually implemented

- deterministic blast-radius preview, computed from the real database or
  resource being mutated, never estimated by a model
- intent-boundary enforcement (`RULE_INTENT_BOUNDARY`)
- recovery-proof verification for the one irreversible action
  (`RULE_RECOVERY_PROOF`)
- cumulative, per-workflow budget enforcement (`RULE_WORKFLOW_BUDGET`)
- fail-closed handling when impact cannot be measured
  (`RULE_UNKNOWN_IMPACT`)
- a generic, registry-based guarded execution boundary (three tools
  across two resource backends, zero tool-specific branches in policy,
  budget, proof, postcondition, or audit code)
- a real MCP stdio gateway (the official `mcp` SDK), not a mock transport
- optional live Nebius semantic extraction with an automatic,
  deterministic fallback on any failure -- **implemented with a
  live-preferred path and documented prior live demonstrations; this
  repository's deterministic verification always uses fallback mode, and
  pytest never calls a live provider**
- optional, explicitly user-triggered live CRAFT evidence retrieval
  (read-only, never authoritative for mutation impact)
- model-generated agent tool proposals against live-discovered MCP
  schemas (documented live experiment in `DEMO.md`)
- one bounded, non-authoritative repair attempt after an eligible BLOCK
- automatic rollback for the eligible hard-delete path after a genuine
  postcondition mismatch, with independent restoration verification
- append-only local audit events, one per governed call
- a database-backed resource (`delete_users`/`deactivate_users`) and a
  filesystem-backed resource (`set_feature_flag`), governed identically
- an offline, deterministic test suite that makes zero external calls

## The three governed tools

| | `delete_users` | `deactivate_users` | `set_feature_flag` |
|---|---|---|---|
| Resource | SQLite `users` table | SQLite `users` table | Filesystem JSON configuration |
| Action | Irreversible hard delete | Reversible status transition | Atomic configuration update |
| Proof required | Yes | No | No (reversible under the local model) |
| Broad request | BLOCK | BLOCK | BLOCK |
| Corrected test request | ALLOW | ALLOW | ALLOW |
| Automatic rollback | Supported in this sandbox, when a validated snapshot is associated with the action | Not used (already reversible) | Not implemented |
| Notes | Restoration synchronizes `test`-environment rows from a broader snapshot, under a single-process assumption -- see `THREAT_MODEL.md` | -- | Impact is the change in effective user exposure, not the requested rollout percentage alone; no real provider is contacted |

## Repository structure

```text
agent/          model proposal and bounded repair (live-only, explicitly labeled)
operations/     deterministic previews, mutations, snapshots, restoration
proofgate/      registry, policy, proofs, budgets, MCP gateway, audit, rollback
scripts/        deterministic demos and local verification
tests/          offline deterministic regression suite
docs/           architecture, trust model, production evolution, interview guide
```

> Operations code performs preview or mutation. It does not decide
> whether an action is allowed.

## Quickstart

Deterministic local setup -- no credentials required for any of this:

```bash
git clone https://github.com/Karthik6412/proofgate.git
cd proofgate

python3 -m venv .venv
source .venv/bin/activate

pip install -e .

python scripts/verify_local_setup.py

pytest -q

streamlit run app.py
# in another terminal, or instead of Streamlit:
python -m proofgate.mcp_server        # start the real MCP stdio gateway
python scripts/run_portfolio_demo.py  # deterministic end-to-end demonstration
```

Reset all generated local state at any time:

```bash
python -c "
from operations.database import reset_working_db
from operations.feature_flags import reset_feature_flags_working
from proofgate.audit import reset_audit_log
reset_working_db(); reset_feature_flags_working(); reset_audit_log()
"
```

### Optional live integrations

Copy `.env.example` to `.env` and fill in real credentials only if you
want to exercise live Nebius or live CRAFT. Neither is required:

- `pytest` never requires live credentials and never calls a live
  provider -- the entire suite runs offline and deterministically.
- Nebius is live-preferred with an automatic deterministic fallback on
  any failure, missing configuration, or malformed response.
- CRAFT live access is explicitly user-triggered from a Streamlit
  button; it is never called automatically, and never from the MCP
  gateway or from tests.
- Missing live integrations fall back honestly -- the system labels
  fallback output as fallback, never as if it were live.

## Deterministic results (final verification run)

| Scenario | Outcome | Triggered rules | Impact |
|---|---|---|---|
| Broad delete | BLOCK | `RULE_INTENT_BOUNDARY`, `RULE_RECOVERY_PROOF`, `RULE_WORKFLOW_BUDGET` | 10,073 |
| Corrected delete without proof | BLOCK | `RULE_RECOVERY_PROOF` | 92 |
| Corrected delete with valid proof | ALLOW | none | 92 |
| Broad deactivate | BLOCK | `RULE_INTENT_BOUNDARY`, `RULE_WORKFLOW_BUDGET` | 10,073 |
| Corrected deactivate | ALLOW | none | 92 |
| One-shot repaired proposal | ALLOW | none on the repaired call | 92 |
| Rollback demonstration | final postcondition `ROLLED_BACK` | genuine postcondition mismatch | 93 actual (92 intended + 1 demo-injected) |
| Broad feature flag | BLOCK | `RULE_INTENT_BOUNDARY`, `RULE_WORKFLOW_BUDGET` | 10,073 |
| Corrected feature-flag enable | ALLOW | none | 92 |
| Corrected feature-flag disable | ALLOW | none | 92 |
| Exact feature-flag no-op | ALLOW, postcondition `VERIFIED` | none | 0 |

`ROLLED_BACK` is a final postcondition state following an *allowed*
execution -- it is not an enforcement verdict. Reproduce this table with
`python scripts/run_portfolio_demo.py`.

As of the final verification run for this slice: **835 deterministic
tests pass**, offline, with zero external calls. See `DEMO.md` for the
exact click paths and per-slice detail.

## Audit and workflow-budget semantics

Every governed call produces exactly one enforcement audit event,
recording the tool, arguments, expected impact, verdict, triggered
rules, proof status, execution result, postcondition, and workflow
budget. Rollback's outcome is represented in that same event's final
postcondition field -- no second, fake enforcement decision is ever
created. The local audit file is append-oriented, but it is **not**
cryptographically chained or externally immutable.

> Workflow budget measures consequential execution, not the final number
> of affected records remaining after recovery.

A normal corrected delete records `92/100`. The rollback demonstration
records `93/100`, because the demonstration's mismatch hook performs one
additional real mutation -- budget honestly reflects the real total
work done, and rollback never refunds it. An exact no-op consumes `0`.

## Further reading

- `docs/architecture.md` -- system and sequence diagrams
- `docs/trust-model.md` -- what each component may and may not control
- `THREAT_MODEL.md` -- protected assets, threats, mitigations, residual risk
- `docs/production-evolution.md` -- plausible future phases, not implemented
- `docs/interview-guide.md` -- explanation aids and an ownership checklist
- `DEMO.md` -- exact click paths, per-slice detail, and every documented number

## Roadmap (not implemented)

- human-in-the-loop approval tier
- a transparent MCP proxy for arbitrary downstream action servers
- additional action types beyond the three currently governed
- see `docs/production-evolution.md` for the fuller production direction

## Development history

This system was developed through a sequence of scoped, test-driven
implementation slices (documented in `DEMO.md`'s own section headers,
numbered through Slice 25.1, plus this final hardening slice). Commit
count does not map one-to-one to slice count -- several slices share a
commit or were checkpointed together.

## Built by

Karthik Nair & Disha, at the Enterprise Agents Hackathon by Emergence AI,
in partnership with Nebius. Hardened afterward into a portfolio-quality
reference implementation.
