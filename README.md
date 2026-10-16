# ProofGate — Action Boundary Reference Implementation

An AI agent should not be authorized on the strength of what it *asks* to
do. ProofGate resolves what a proposed action would actually touch, checks
coarse permissions and the human's delegated authority against that
resolved effect, and binds any authorization to a digest of the exact
action evaluated — so an action altered after authorization fails
verification.

The repository contains two governed domains that share one deterministic
policy engine:

- **Legal — Action Boundary v1** (this README's focus): a multi-step agent
  plan over a synthetic legal corpus, resolved, permission-checked,
  authorized against an `AuthorityGrant`, and digest-bound. It stops at
  authorization; nothing is executed.
- **Operations — the original ProofGate**: guarded execution of three
  consequential tools with impact preview, recovery proofs, postcondition
  verification and rollback. See [Operations domain](#operations-domain).

ProofGate is a standalone reference implementation on synthetic data. It is
not production-ready infrastructure; see `THREAT_MODEL.md`.

## The example: permission passes, authority does not

The human asks:

```text
Prepare the diligence package for Matter M-102 and share the
non-privileged documents with outside counsel.
```

and grants authority for **matter M-102, at most 100 documents, privilege
excluded, recipient `counsel@example.com`**. The agent proposes a plan that
looks entirely reasonable:

```text
1. search_documents(matter="M-102", query="diligence")
2. compile_package(documents=$step1.result, include_related=True)
3. share_package(package=$step2.result, recipient="counsel@example.com")
```

All four coarse permission checks pass: the user may access the matters
involved, the agent may search and package, and the recipient is
permitted. Then the trusted resolver computes what the plan would actually
share. The search stays inside M-102; following related-document links
does not:

| | Authorized | Resolved |
|---|---|---|
| Matters | M-102 | M-102: 126, **M-118: 17** |
| Documents | ≤ 100 | **143** |
| Privileged | 0 (excluded) | **9** |
| Restricted (ethical wall) | — | **4** |
| Recipients | counsel@example.com | counsel@example.com |

| Legal boundary | Result |
|---|---|
| Matter boundary | FAIL |
| Privilege boundary | FAIL |
| Workflow budget | FAIL |
| Recipient boundary | PASS |

**Decision: BLOCK. No authorization is issued.**

**Permission to use tools or access data is not authority to perform the
resolved action.** Every coarse check was correct — the user can access
M-118 — yet this workflow was only authorized for M-102. The violation is
visible only in the resolved effect, never in the plan text.

## Run the demo

From the repository root:

```bash
source .venv/bin/activate
python scripts/legal_demo.py
```

No arguments, API key, network access or model call is required. The demo
plays the spec's seven beats from seeded local fixtures and prints the same
output on every run: the request, the grant, the plan, four permission
PASS lines, the authorized-vs-resolved effect, three Legal FAILs and one
PASS with a BLOCK, and finally a clean in-scope plan that is authorized and
then rejected once its package is altered. An excerpt:

```text
BEAT 4 -- Permission checks
  PASS  User may access matters  M-102, M-118
  PASS  Agent may search         search_documents
  PASS  Agent may package        compile_package
  PASS  Recipient permitted      counsel@example.com
  TOOL AND MATTER PERMISSIONS PASS

BEAT 6 -- Verdict
  Permission:  PASS (all four coarse checks)
  Authority / Legal boundaries:
    FAIL  matter_boundary      Consequential documents span matters ['M-118'] outside authorized matter M-102.
    FAIL  privilege_boundary   9 privileged documents are consequential but the grant excludes privileged content.
    FAIL  workflow_budget      143 unique consequential documents exceed the 100-document workflow budget.
    PASS  recipient_boundary
  DECISION: BLOCK

BEAT 7 -- Tampered execution
  After authorization, the package is altered: + D-118-001 (M-118), 80 -> 81 documents.
  DIGEST MISMATCH -- AUTHORIZATION FAILS
```

Every printed fact and verdict comes from the production code paths; the
script only orchestrates and formats. Decision records from a demo run go
to a temporary directory.

## How Action Boundary v1 works

```text
Human request + AuthorityGrant
  -> agent plan (proposed, untrusted)
  -> trusted resolver            what would this actually touch?
  -> coarse permission checks    may this user/agent do this at all?
  -> four Legal rules            does it fit the human's authority?
  -> authorization binding       AuthorizationRecord + action digest
  -> decision record             append-only JSON line
  -> digest verification         is the presented action the authorized one?
```

Grant validity is checked first: a missing, not-yet-valid or expired grant
fails closed before anything is resolved.

**Trusted resolver.** Executes read-only steps and resolves the targets of
side-effecting ones without performing them: searches run, package
membership is resolved (following exactly one hop of related-document
links), and the share resolves its recipient but sends nothing. The output
separates documents merely read from the *consequential* documents the
share would act on, and carries their matters, privilege and restriction
labels, content hashes, recipients and counts. The resolver may inspect
metadata the agent never sees; raw document text never leaves the source
documents. Unknown tools or unresolvable plans fail closed.

**Coarse permission checks.** Four explicit checks, computed after
resolution against the trusted `PermissionSnapshot` (IAM / ethical-wall
state) and a separate agent capability set: the user may access every
resolved matter, the agent may search, the agent may package, and every
recipient is permitted. Any failure fails closed before the Legal rules
run. These checks do not waive document-level restrictions: a restricted
consequential document vetoes ALLOW on its own, independently of the four
lines. For the synthetic demo, agent capabilities are a seeded local set.

**Four Legal rules.** Run by the generic `evaluate_policy` engine — the same
engine the Operations domain uses — comparing the resolved consequential
effect against the `AuthorityGrant`: matter boundary, privilege boundary,
workflow budget (unique consequential documents, reads excluded, with
previously committed IDs counted once) and recipient boundary. Any FAIL is
a BLOCK. Each outcome carries machine-readable evidence (authorized value,
resolved value, reason).

**Authorization binding.** An ALLOW, and only an ALLOW, produces an
immutable `AuthorizationRecord` holding the action digest, grant and
permission-snapshot digests, issue time and the grant's expiry. The action
digest is a SHA-256 over canonical JSON of the execution-relevant
semantics: every grant field, every plan step and argument, the
consequential document IDs, each one's matter, labels and content hash,
the recipients, and the permission-snapshot digest. Representation-only
differences (argument order, equivalent timezones) do not change it.

**Decision record.** Every action that reaches the Legal rules appends one
JSON line — authorized vs resolved values, the four checks as PASS/FAIL
with their evidence, the decision and the action digest — with fixed
`"executed": false` and `"postcondition": null`, because v1 executes
nothing.

**Digest verification.** `verify_authorization` recomputes the digest from
the presented action and raises `AuthorizationDigestMismatch` on any
difference. It never re-authorizes.

## The tamper check

The demo's in-scope plan (the same request without related-document
expansion) authorizes 80 M-102 documents and receives an
`AuthorizationRecord`. The untouched action verifies. Then one document,
`D-118-001` from matter M-118, is slipped into the package: the presented
action now covers 81 documents, its digest differs, and verification
fails with `AuthorizationDigestMismatch`.

Authorization applies to the exact action that was evaluated — its
resources, labels, recipients, plan and grant — not to a workflow ID or a
plan label. This detects alteration of an authorized action; it is not a
defense against every attack (see the limitations below).

## What Friday v1 guarantees

Within this repository and its seeded local environment, verified by the
test suite:

- missing, not-yet-valid or expired grants, missing permission state,
  unknown tools and unresolvable plans fail closed; none can produce ALLOW
- the four coarse permission checks are explicit, deterministic, computed
  from the resolved effect, and run before the Legal rules
- restricted consequential documents veto ALLOW
- the four Legal rules evaluate the trusted resolved effect against the
  grant, never the plan's requested scope
- only an ALLOW produces an `AuthorizationRecord`, bound to a canonical
  action digest
- changing any execution-relevant part of an authorized action — plan,
  arguments, resources, labels, recipients, grant or permission snapshot —
  fails verification
- decision records are append-only JSONL, kept separate from the
  Operations audit log
- raw document text and canary markers never appear in the resolved
  effect, policy context, rule evidence, `LegalAction`,
  `AuthorizationRecord`, decision record or demo output
- the demo needs no network, model, API key or external service, and
  beats 1–7 run deterministically

## Friday v1 limitations

Action Boundary v1 **stops at authorization**.

- **No real execution.** Nothing is sent, shared or mutated. The share step
  is resolved, never performed.
- **No permission re-fetch or staleness handling.** The permission
  snapshot is trusted input supplied to the flow. Verification does not
  re-fetch IAM or ethical-wall state, re-resolve the plan, or detect a
  permission revoked after authorization. It compares the presented action
  as given.
- **Agent capabilities are not in the action digest.** The capability set
  is checked before authorization but is not itself bound into the digest.
- **Authorization expiry is stored, not enforced.** `AuthorizationRecord`
  carries the grant's expiry; checking it belongs to a future execution
  sequence.
- **Restricted documents are a separate veto**, not a fifth permission
  check: the four PASS lines alone do not mean every document may be
  shared.
- **No approval layer.** There is no `REQUIRE_APPROVAL` verdict or
  `ApprovalRecord`; the verdict is binary ALLOW/BLOCK.
- **No execution, outbox or postcondition layer** for Legal actions: no
  dispatch, outbox, idempotent execution, retries, partial-failure
  handling, postcondition verification, rollback or compensation.
- **Pre-policy failures have no decision record.** Grant, permission-state
  and resolution failures are reported by the authorization attempt's
  status, not in the Legal JSON decision-record schema.
- **A digest mismatch is not audited.** `verify_authorization` raises
  `AuthorizationDigestMismatch` naming the expected and actual digests, but
  no record of the failed verification is written.
- **Persistent budget state is not implemented.** Previously committed
  document IDs are supplied in memory by the caller.
- **Synthetic sources.** The corpus, grants, permission snapshot and agent
  capabilities are seeded local fixtures. The architecture is implemented;
  integrations with real IAM, ethical-wall or document systems are not.
- **Per-tool resolvers.** Each tool needs a resolver implementation, and a
  wrong resolver would yield a wrong verdict.

## Post-Friday continuation (not implemented)

Designed in `docs/action-boundary-final-build-spec.md`, not shipped:

- permission re-fetch, re-resolution and staleness handling before execution
- persistent cross-workflow unique-document budget semantics
- human approval bound to the same action digest
- simulated local execution with an outbox record
- identity-level postcondition verification of the executed record
- a labeled canary fault-injection scenario for read-side leakage
- a v2 design based on transactional rehearsal rather than per-tool prediction

## Where to look

| Path | Purpose |
|---|---|
| `legal/models.py` | `Document`, `AuthorityGrant`, `PermissionSnapshot`, `AgentCapabilities` |
| `legal/corpus.py` | the seeded synthetic corpus and its designed link structure |
| `legal/permissions.py` | seeded permission-snapshot and agent-capability providers |
| `proofgate/bindings/legal/resolver.py` | trusted read-only resolver and `LegalResolvedEffect` |
| `proofgate/bindings/legal/permission_checks.py` | the four coarse checks and the restricted veto |
| `proofgate/bindings/legal/rules.py` | the four Legal rules on the generic engine |
| `proofgate/bindings/legal/authorize.py` | the authorization pipeline and its fail-closed statuses |
| `proofgate/bindings/legal/authorization.py` | action digest, `AuthorizationRecord`, `verify_authorization` |
| `proofgate/bindings/legal/decision_record.py` | the append-only Legal decision record |
| `proofgate/policy.py` | the domain-generic Rule/RuleSet policy engine |
| `proofgate/selector.py` | shared canonical JSON and SHA-256 digest primitives |
| `scripts/legal_demo.py` | the beats 1–7 demo |
| `docs/action-boundary-final-build-spec.md` | the Action Boundary specification |

## Operations domain

The original ProofGate governs three consequential tools through a guarded
execution pipeline. Everything in this section describes that domain; none
of it — execution, postconditions, rollback — applies to Action Boundary v1.

### The failure it exists to stop

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

### What the Operations pipeline does

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

### Why this differs from prompt-based safety

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

### What's implemented in the Operations domain

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

### The three governed tools

| | `delete_users` | `deactivate_users` | `set_feature_flag` |
|---|---|---|---|
| Resource | SQLite `users` table | SQLite `users` table | Filesystem JSON configuration |
| Action | Irreversible hard delete | Reversible status transition | Atomic configuration update |
| Proof required | Yes | No | No (reversible under the local model) |
| Broad request | BLOCK | BLOCK | BLOCK |
| Corrected test request | ALLOW | ALLOW | ALLOW |
| Automatic rollback | Supported in this sandbox, when a validated snapshot is associated with the action | Not used (already reversible) | Not implemented |
| Notes | Restoration synchronizes `test`-environment rows from a broader snapshot, under a single-process assumption -- see `THREAT_MODEL.md` | -- | Impact is the change in effective user exposure, not the requested rollout percentage alone; no real provider is contacted |

These are the tools registered in the Operations registry. Legal actions
do not go through the registry or the guarded execution pipeline; the two
domains share the policy engine and hashing/audit primitives.

### Deterministic results

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

### Audit and workflow-budget semantics

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

## Repository structure

```text
legal/              synthetic Legal domain facts: corpus, grants, permissions, capabilities
proofgate/          generic policy engine, canonical hashing, audit, registry, MCP gateway
proofgate/bindings/ domain bindings: operations.py (Operations), legal/ (Action Boundary)
operations/         deterministic previews, mutations, snapshots, restoration
agent/              model proposal and bounded repair (live-only, explicitly labeled)
scripts/            deterministic demos and local verification
tests/              offline deterministic regression suite
docs/               specification, architecture, trust model, production evolution
```

> Domain code (`legal/`, `operations/`) supplies facts or performs preview
> and mutation. It never decides whether an action is allowed.

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

python scripts/legal_demo.py          # Action Boundary v1, beats 1-7

streamlit run app.py                  # Operations domain UI
# in another terminal, or instead of Streamlit:
python -m proofgate.mcp_server        # start the real MCP stdio gateway
python scripts/run_portfolio_demo.py  # deterministic Operations demonstration
```

Reset all generated Operations state at any time:

```bash
python -c "
from operations.database import reset_working_db
from operations.feature_flags import reset_feature_flags_working
from proofgate.audit import reset_audit_log
reset_working_db(); reset_feature_flags_working(); reset_audit_log()
"
```

### Optional live integrations (Operations domain)

Copy `.env.example` to `.env` and fill in real credentials only if you
want to exercise live Nebius or live CRAFT. Neither is required, and
Action Boundary v1 uses neither:

- `pytest` never requires live credentials and never calls a live
  provider -- the entire suite runs offline and deterministically.
- Nebius is live-preferred with an automatic deterministic fallback on
  any failure, missing configuration, or malformed response.
- CRAFT live access is explicitly user-triggered from a Streamlit
  button; it is never called automatically, and never from the MCP
  gateway or from tests.
- Missing live integrations fall back honestly -- the system labels
  fallback output as fallback, never as if it were live.

## Tests

`pytest -q` runs the whole suite offline with zero external calls; as of
Action Boundary v1, **1219 tests pass**. Action Boundary coverage includes:

- resolver behavior: one-hop expansion, read vs consequential documents,
  determinism, read-only proof and deep immutability
- the four Legal rules, including budget boundaries and union semantics
- the permission boundary: each coarse check, the restricted veto, and
  missing permission state
- the authorization pipeline's fail-closed statuses and grant validity window
- action-digest determinism, a pinned digest, and tamper rejection
- decision-record shape and append-only persistence
- the restricted-text boundary, with structural guards against raw-text
  access and model-client imports
- the demo: beat order, real results, identical output across runs, and no
  network access

Operations behavior is pinned by golden-parity tests that the shared
policy-engine refactor left unchanged.

## Further reading

- `docs/action-boundary-final-build-spec.md` -- the Action Boundary specification
- `docs/architecture.md` -- system and sequence diagrams
- `docs/trust-model.md` -- what each component may and may not control
- `THREAT_MODEL.md` -- protected assets, threats, mitigations, residual risk
- `docs/production-evolution.md` -- plausible future phases, not implemented
- `DEMO.md` -- exact click paths, per-slice detail, and every documented number

## Roadmap (not implemented)

- Action Boundary continuation scope: see [Post-Friday continuation](#post-friday-continuation-not-implemented)
- a transparent MCP proxy for arbitrary downstream action servers
- additional Operations action types beyond the three currently governed
- see `docs/production-evolution.md` for the fuller production direction

## Development history

The Operations domain was developed through scoped, test-driven slices
(documented in `DEMO.md`'s section headers, numbered through Slice 25.1,
plus a final hardening slice). Action Boundary v1 followed as a further
series of slices: inverting the tool registry, parameterizing the policy
engine by domain ruleset, then the Legal facts, resolver, rules,
authorization binding, decision record, permission boundary and demo.
Commit count does not map one-to-one to slice count.

## Built by

Karthik Nair & Disha Anil, at the Enterprise Agents Hackathon by Emergence AI,
in partnership with Nebius. Hardened afterward into a portfolio-quality
reference implementation, then extended with Action Boundary v1.
