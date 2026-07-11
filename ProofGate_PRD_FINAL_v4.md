# PROOFGATE — Blast-Radius Enforcement for AI Agents
## Hackathon PRD — FINAL v4

**Version:** MVP-FINAL-v4  
**Team:** 1–2 builders  
**Build window:** approximately 5 focused hours  
**Primary judging goals:** creativity, real-world impact, completeness, production-grade polish

---

# 0. EXECUTIVE SUMMARY

ProofGate is a runtime enforcement layer for consequential AI-agent actions.

Traditional access control answers:

> Is this agent allowed to call this tool?

ProofGate answers:

> Is this exact call, with these exact arguments, dangerously broad, inconsistent with the user’s intent, or insufficiently recoverable?

## Demo failure

**User intent**

> Clean up inactive **test** accounts that have not logged in for 90 days.

**Proposed action**

```python
delete_users(inactive_days=90)
```

The upstream agent accidentally omits:

```python
environment="test"
```

**Measured effect if executed**

- 10,073 total users affected
- 9,981 production users
- 92 test users

The tool itself is authorized. The arguments are catastrophic.

## ProofGate loop

```text
Intent
→ proposed action
→ operational impact preview
→ semantic risk extraction with Nebius
→ deterministic policy
→ ALLOW or BLOCK
→ machine-readable repair
→ execution-bound rollback proof
→ safe execution
→ postcondition verification
→ audit trail
```

The LLM never makes the final security decision.

- **CRAFT by Emergence** provides governed enterprise context and read-only analytical evidence.
- **Nebius Token Factory** powers intent extraction and structured semantic-risk extraction.
- **ProofGate’s deterministic Python policy engine** makes the final ALLOW/BLOCK decision.
- **The Operations sandbox** provides the authoritative preflight and performs the real disposable mutation.

## Core positioning

> Tool authorization is binary. Operational risk is continuous.

> Dangerous actions need proof, not promises.

---

# 1. PROBLEM

Enterprise agents are moving from answering questions to changing real systems:

- CRMs
- customer communication tools
- billing systems
- databases
- cloud infrastructure
- support platforms
- internal operations tools

Most controls operate at the tool level:

```text
Agent may call delete_users
Agent may call send_campaign
Agent may call issue_refund
```

Operational risk exists at the invocation level:

```text
delete 2 test users
```

is radically different from:

```text
delete every inactive user across production
```

A fully authorized action becomes catastrophic when one scope constraint is:

- omitted
- misunderstood
- mistranslated
- dropped during tool-call generation
- inferred incorrectly
- changed between preview and execution

Existing approaches are insufficient:

1. **Human approval for every write**  
   Safe, but defeats agent autonomy.

2. **Trust the agent prompt**  
   Fast, but not enforceable.

3. **Static allowlists**  
   Useful for access control, but blind to argument-level blast radius.

4. **LLM-as-judge**  
   Flexible, but inappropriate as the sole security authority.

ProofGate introduces measured, deterministic, action-specific runtime enforcement.

---

# 2. PRODUCT THESIS

A production-grade agent-control layer must understand:

- user-intended scope
- proposed executable scope
- exact affected-record count
- production versus test boundaries
- reversibility
- proof of recovery
- cumulative workflow impact
- actual post-execution impact
- complete audit evidence

ProofGate sits at the boundary between:

## Intelligence

The agent understands enterprise data and decides what it wants to do.

## Activation

The agent attempts to modify a real operational system.

## Control Plane

ProofGate determines whether the action is bounded, aligned with intent, recoverable, and auditable.

The product is not merely a generic MCP proxy.

Its differentiating runtime primitive is:

> Compare user intent with exact tool arguments, measure the real operational blast radius, and require action-bound recovery evidence before irreversible execution.

---

# 3. HACKATHON GOAL

Build one complete and polished action-safety loop.

The demo must prove:

1. An authorized tool can still receive catastrophic arguments.
2. ProofGate detects a missing user-intent constraint.
3. Blast radius is measured using a real operational preflight.
4. Nebius extracts semantic risk but does not issue the verdict.
5. Deterministic Python policy blocks the unsafe invocation.
6. The agent receives structured repair guidance.
7. The repaired action uses the same destructive tool with corrected scope.
8. The agent provides execution-bound rollback proof.
9. ProofGate allows the corrected invocation.
10. Actual impact is verified after execution.
11. The whole lifecycle is logged.
12. The presentation requires no more than three deliberate user interactions.

---

# 4. NON-GOALS

Do not build these during the MVP:

- full transparent proxying of the CRAFT MCP server
- enterprise IAM integration
- real production mutations
- a universal policy language
- Kubernetes deployment
- multiple destructive scenarios
- blockchain
- full cryptographic PKI
- multi-tenant SaaS
- complex human-approval workflows
- automated policy learning
- real customer email delivery
- a full generalized security gateway

One polished scenario is the entire product for this hackathon.

---

# 5. PRIMARY DEMO

## 5.1 User request

> Clean up inactive test accounts that have not logged in for 90 days.

## 5.2 Reliable upstream failure

For demo reliability, the application deliberately simulates a common agent failure:

```text
Dropped constraint: environment="test"
```

The UI must label this honestly:

> Simulating an upstream agent scope-loss failure.

The proposed action becomes:

```python
delete_users(inactive_days=90)
```

No verdict is hard-coded. ProofGate still performs live preview, Nebius analysis, deterministic policy, proof verification, execution, and postcondition verification.

---

## 5.3 Demo interaction budget

The complete demo should require at most three deliberate interactions:

1. **Run Unprotected**
2. **Reset and Run Protected**
3. Optional: expand the audit/proof details if a judge asks

The protected flow automatically:

- previews impact
- blocks the unsafe call
- generates structured repair
- creates a snapshot
- validates proof
- retries the corrected call
- executes
- verifies the postcondition

Snapshot creation remains real, but does not need to be narrated as a separate live beat. The trace may show it quietly, while the main UI jumps from BLOCK to a populated `PROOF VALID` panel.

---

## 5.4 Run 1 — UNPROTECTED

The agent bypasses ProofGate and invokes the sandbox operation directly.

Expected result:

```text
10,073 users deleted
9,981 production users deleted
92 test users deleted
```

The local SQLite database is disposable and is reset immediately.

Narration:

> The tool was authorized. The arguments were catastrophic.

---

## 5.5 Run 2 — PROTECTED

The same instruction and the same bad proposed call are routed through ProofGate.

### Step A — CRAFT enterprise context

CRAFT performs a real read-only workflow:

1. discovers the relevant e-commerce/customer schema
2. generates SQL from natural language
3. executes a count or cohort query
4. surfaces the returned evidence in the trace

CRAFT establishes:

- customer/account data context
- inactive-cohort definition
- enterprise evidence used by the agent

Important:

> CRAFT provides the cohort definition and enterprise context.  
> The Operations preflight is the source of truth for the exact mutation blast radius.

### Step B — Operational preflight

The shadow CRM sandbox calculates:

```text
Total affected:       10,073
Production affected:   9,981
Test affected:            92
Action:             hard delete
Proof:                 absent
```

### Step C — Nebius semantic risk extraction

Nebius compares:

```text
User requested:
inactive test accounts
```

against:

```text
Agent proposed:
all inactive accounts
```

Nebius returns structured features only:

```json
{
  "intent_mismatch": true,
  "missing_constraints": ["environment=test"],
  "scope_class": "mass",
  "production_impact": true,
  "irreversible": true,
  "uncertainty": 0.03,
  "rationale": [
    "The requested environment constraint is absent.",
    "The measured action includes production users."
  ]
}
```

Nebius does not return `ALLOW` or `BLOCK`.

### Step D — Deterministic policy

ProofGate returns:

```text
VERDICT: BLOCK
RISK SCORE: 9.6 / 10
```

The UI must distinguish two concepts.

#### Risk factors

These explain severity:

- mass scope
- production impact
- intent mismatch
- irreversible action
- missing proof

#### Triggered policy rules

These explain the actual enforcement decision:

```text
RULE_INTENT_BOUNDARY
The user requested test scope, but production rows are included.

RULE_RECOVERY_PROOF
The irreversible action has no valid rollback proof.

RULE_WORKFLOW_BUDGET
10,073 projected mutations exceed the 100-row workflow budget.
```

Do not display rules that did not trigger.

`RULE_UNKNOWN_IMPACT` does not trigger because the preview succeeded.

### Step E — Structured repair

ProofGate returns machine-readable repair guidance:

```json
{
  "missing_requirements": [
    "environment=test",
    "valid rollback proof"
  ],
  "suggested_repair": {
    "tool": "delete_users",
    "arguments": {
      "inactive_days": 90,
      "environment": "test"
    },
    "next_step": "create_snapshot"
  }
}
```

### Step F — Corrected preflight

The agent retries the same intended tool scope:

```python
delete_users(
    inactive_days=90,
    environment="test"
)
```

New measured impact:

```text
Total affected:       92
Production affected:   0
Test affected:        92
```

### Step G — Execution-bound proof

The orchestrator automatically requests:

```python
create_snapshot(
    resource="users",
    selector={
        "inactive_days": 90,
        "environment": "test"
    },
    max_affected_rows=92
)
```

ProofGate receives:

```json
{
  "snapshot_id": "snap-demo-001",
  "resource": "users",
  "selector_hash": "<sha256>",
  "max_affected_rows": 92
}
```

ProofGate verifies:

1. the snapshot exists
2. the proof resource is `users`
3. the proof selector hash matches the exact proposed arguments
4. measured count is not greater than `max_affected_rows`

The main UI can show the completed panel directly:

```text
RECOVERY PROOF: VALID

Snapshot exists       ✓
Resource matches      ✓
Selector hash matches ✓
92 ≤ approved 92      ✓
```

The trace may still show:

```text
Operations.create_snapshot    SUCCESS
```

### Step H — Allow and execute

The agent retries through the ProofGate action boundary:

```python
proofgate.delete_users(
    action_context=context,
    inactive_days=90,
    environment="test",
    rollback_proof=proof
)
```

ProofGate returns:

```text
VERDICT: ALLOW
```

The same public tool receives a different verdict because:

- the arguments now match intent
- production scope is zero
- blast radius is within budget
- rollback proof is bound to the exact action

### Step I — Postcondition verification

After execution:

```text
Predicted affected:    92
Actual affected:       92
Production affected:    0
Verification:     VERIFIED
```

Closing metrics:

```text
Actions evaluated:                 6+
Catastrophic actions blocked:       1
Safe test accounts deleted:        92
Production accounts affected:       0
Workflow mutation budget used: 92/100
Audit trail:                 complete
```

---

# 6. ARCHITECTURE

```text
User instruction
      │
      ▼
Agent Orchestrator
Reliable deterministic sequence
Nebius performs live extraction
      │
      ├─────────────────────────────────────────────┐
      │                                             │
      ▼                                             ▼
CRAFT MCP                                   Proposed Action
Read-only enterprise context                       │
get_schema                                         ▼
generate_sql                                 ProofGate Boundary
execute_query                                guarded public API
      │                                             │
      └──────────── enterprise evidence ───────────►│
                                                    │
                                                    ├─ intent extraction: Nebius
                                                    ├─ operational preflight
                                                    ├─ risk extraction: Nebius
                                                    ├─ deterministic policy
                                                    ├─ workflow budget
                                                    ├─ rollback-proof verification
                                                    └─ JSONL audit
                                                           │
                                                 ALLOW or BLOCK
                                                           │
                                                           ▼
                                                Operations Module
                                                dumb SQLite mutation
                                                           │
                                                           ▼
                                               Postcondition Verifier
                                                           │
                                                           ▼
                                                    Streamlit UI
```

---

# 7. CONTRACT BOUNDARY — FINAL DECISION

This contract must not change during implementation.

## 7.1 Public ProofGate action boundary

The agent calls:

```python
guarded_delete_users(
    action_context: ActionContext,
    inactive_days: int,
    environment: str | None,
    rollback_proof: RollbackProof | None
) -> EnforcementResult
```

The MCP-facing tool may be named:

```text
delete_users
```

but it routes internally through `guarded_delete_users`.

Responsibilities:

- run preview
- validate intent
- call Nebius risk extraction
- evaluate deterministic policy
- validate proof
- enforce workflow budget
- write audit event
- call the dumb Operations mutation only after ALLOW
- verify postconditions

## 7.2 Dumb Operations mutation

The internal Operations function remains policy-unaware:

```python
operations.delete_users(
    inactive_days: int,
    environment: str | None
) -> MutationResult
```

It does not accept:

- rollback proof
- user intent
- policy configuration
- risk score
- ALLOW/BLOCK verdict

Only the guarded ProofGate boundary may call it in protected mode.

## 7.3 Direct mode

UNPROTECTED mode intentionally calls:

```python
operations.delete_users(...)
```

directly.

This demonstrates what happens without the enforcement boundary.

## Contract statement

> `rollback_proof` belongs to the ProofGate action boundary, not to the underlying Operations mutation function.

---

# 8. MCP STRUCTURE

## Required

CRAFT is used through its real MCP server.

## ProofGate transport

Build enforcement logic as a plain Python module first.

Then add a thin FastMCP adapter exposing:

- `preview_delete_users`
- `create_snapshot`
- `delete_users`

The UI may call the core directly for maximum demo reliability, but at least one ProofGate operation should be invocable through MCP before submission if time permits.

Production architecture:

```text
Agent → ProofGate MCP Gateway → arbitrary downstream action MCP servers
```

Hackathon architecture:

```text
Agent → ProofGate core/thin MCP adapter → local shadow CRM
```

---

# 9. SPONSOR INTEGRATION

## 9.1 Emergence CRAFT

CRAFT is the governed intelligence plane.

Minimum live integration:

1. connect successfully
2. discover e-commerce/customer schema
3. call `generate_sql` from a natural-language question
4. execute one read-only query
5. display returned evidence in the action trace and audit event

Correct claim:

> CRAFT provides enterprise context and read-only impact intelligence.

Incorrect claim:

> CRAFT performs the deletion.

CRAFT and the operational preflight have separate responsibilities.

### CRAFT

- defines or discovers the business cohort
- provides schema and enterprise evidence
- supports the agent’s reasoning

### Operations preflight

- queries the system that will actually be mutated
- produces authoritative exact counts
- separates production and test impact
- creates the ImpactEnvelope

## 9.2 Nebius Token Factory

Nebius is a required live component.

Use the OpenAI-compatible endpoint:

```text
https://api.tokenfactory.nebius.com/v1/
```

Recommended model:

```text
nvidia/nemotron-3-super-120b-a12b
```

Nebius performs two structured inference tasks.

### Task 1 — Intent extraction

Input:

- original user instruction

Output:

```json
{
  "action_type": "delete_users",
  "target_resource": "users",
  "environment": "test",
  "inactivity_days": 90,
  "confidence": 0.98
}
```

### Task 2 — Risk-feature extraction

Input:

- original instruction
- extracted intent
- proposed action
- measured ImpactEnvelope

Output:

```json
{
  "intent_mismatch": true,
  "missing_constraints": ["environment=test"],
  "scope_class": "mass",
  "production_impact": true,
  "irreversible": true,
  "uncertainty": 0.03,
  "rationale": []
}
```

Strict Nebius prompt rules:

- JSON only
- no final verdict
- never invent counts
- use ImpactEnvelope as the source of truth
- extract semantic features only

Python deterministic rules make the final decision.

## 9.3 Fallbacks

Nebius fallback:

- regex extraction for `test`, `production`, and inactivity-day values
- consequential action fails closed if intent remains unknown

CRAFT fallback:

- 25-minute authentication timebox
- cached evidence may be shown only if clearly labeled:
  `Previously retrieved CRAFT evidence`
- never imply that a cached result was live

Live CRAFT is strongly preferred.

---

# 10. DATA MODEL

## 10.1 Shadow CRM SQLite table

```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL,
    environment TEXT NOT NULL,
    last_login TEXT NOT NULL,
    status TEXT NOT NULL,
    deleted_at TEXT,
    row_version INTEGER NOT NULL
);
```

## 10.2 Deterministic seed

```text
9,981 inactive production users
   92 inactive test users
  500 active production users
   50 active test users
```

Reset:

```text
copy pristine.db → working.db
```

No random variation during the demo.

---

# 11. CORE CONTRACTS

## 11.1 IntentConstraints

```python
class IntentConstraints(BaseModel):
    action_type: str
    target_resource: str
    environment: str | None
    inactivity_days: int | None
    confidence: float
```

## 11.2 ActionContext

```python
class ActionContext(BaseModel):
    workflow_id: str
    requesting_user: str
    agent_id: str
    original_instruction: str
```

## 11.3 ImpactEnvelope

```python
class ImpactEnvelope(BaseModel):
    tool_name: str
    estimated_count: int
    environment_counts: dict[str, int]
    hard_delete: bool
    reversibility: str
    selector_hash: str
    generated_at: str
```

The selector hash is SHA-256 over canonical JSON action arguments.

## 11.4 RiskFeatures

```python
class RiskFeatures(BaseModel):
    intent_mismatch: bool
    missing_constraints: list[str]
    scope_class: str
    production_impact: bool
    irreversible: bool
    uncertainty: float
    rationale: list[str]
```

## 11.5 RollbackProof

```python
class RollbackProof(BaseModel):
    snapshot_id: str
    resource: str
    selector_hash: str
    max_affected_rows: int
```

## 11.6 TriggeredRule

```python
class TriggeredRule(BaseModel):
    rule_id: str
    explanation: str
```

## 11.7 EnforcementResult

```python
class EnforcementResult(BaseModel):
    verdict: Literal["ALLOW", "BLOCK"]
    risk_score: float
    risk_factors: list[str]
    triggered_rules: list[TriggeredRule]
    missing_requirements: list[str]
    suggested_repairs: list[dict]
    executed: bool
    audit_event_id: str
```

## 11.8 MutationResult

```python
class MutationResult(BaseModel):
    affected_count: int
    production_affected: int
    test_affected: int
```

## 11.9 PostconditionResult

```python
class PostconditionResult(BaseModel):
    predicted_count: int
    actual_count: int
    production_affected: int
    status: Literal[
        "VERIFIED",
        "MISMATCH",
        "ROLLED_BACK",
        "MANUAL_REVIEW_REQUIRED"
    ]
```

---

# 12. DETERMINISTIC POLICY

Nebius never returns the verdict.

The Python policy engine uses measured facts and extracted semantic features.

## 12.1 Hard-block rules

Any triggered rule produces `BLOCK`.

### RULE_INTENT_BOUNDARY

```text
Intent requires environment=test
AND preview contains production rows
```

### RULE_RECOVERY_PROOF

```text
Action is irreversible
AND no valid action-bound rollback proof exists
```

### RULE_UNKNOWN_IMPACT

```text
Impact preview failed or is unknown
AND tool is consequential
```

Fail closed.

### RULE_WORKFLOW_BUDGET

```text
rows_mutated_so_far + proposed_affected_rows
> MAX_MUTATED_ROWS_PER_WORKFLOW
```

MVP configuration:

```text
MAX_MUTATED_ROWS_PER_WORKFLOW = 100
MAX_PRODUCTION_DELETIONS = 0
```

## 12.2 Proof validation

Proof is valid only if:

1. snapshot exists
2. resource matches
3. selector hash matches exact canonical arguments
4. estimated count ≤ max affected rows

A valid proof does not override intent mismatch.

Proof is recoverability, not authorization.

## 12.3 Allow rules

An action may be allowed only when:

- intent matches the proposed arguments
- no forbidden production impact exists
- exact impact is known
- projected cumulative impact is within budget
- irreversible action has valid proof

## 12.4 UI correctness rule

The UI must display only:

- risk factors actually present in the RiskFeatures/ImpactEnvelope
- policy rules actually returned in `triggered_rules`

Never render a canned list of reasons.

---

# 13. RISK SCORE

The score is explanatory.

Hard rules determine the verdict.

Suggested components:

```text
Scope:
  0–10 rows          +0.5
  11–100 rows        +1.0
  101–1,000 rows     +2.0
  >1,000 rows        +3.0

Production impact    +2.0
Intent mismatch      +2.0
Irreversible         +1.5
Missing proof        +1.0
Uncertainty          +0.0 to +1.0
```

Cap at 10.

Only add `missing proof` when proof is required and invalid or absent.

---

# 14. WORKFLOW BUDGET

Track real cumulative mutated rows.

```python
class WorkflowBudget(BaseModel):
    workflow_id: str
    rows_mutated: int = 0
    production_rows_mutated: int = 0
```

Before action:

```python
projected = rows_mutated + impact.estimated_count
```

Block if:

```python
projected > 100
```

After successful verified execution:

```python
rows_mutated += postcondition.actual_count
```

UI:

```text
Workflow mutation budget: 92 / 100
```

This prevents fragmentation attacks using many individually small calls.

---

# 15. OPERATIONS MODULE

Required functions:

```python
preview_delete_users(
    inactive_days: int,
    environment: str | None
) -> ImpactEnvelope
```

```python
create_snapshot(
    resource: str,
    inactive_days: int,
    environment: str | None,
    max_affected_rows: int
) -> RollbackProof
```

```python
delete_users(
    inactive_days: int,
    environment: str | None
) -> MutationResult
```

```python
verify_postcondition(
    before_state,
    after_state,
    impact: ImpactEnvelope
) -> PostconditionResult
```

```python
reset_demo_database() -> None
```

Protected deletion may only reach `operations.delete_users` after the guarded boundary returns ALLOW.

---

# 16. AUDIT TRAIL

Use append-only JSONL.

Do not claim tamper resistance or immutability.

Each event should include:

```json
{
  "event_id": "...",
  "workflow_id": "...",
  "timestamp": "...",
  "original_instruction": "...",
  "tool_name": "delete_users",
  "tool_arguments": {},
  "intent_constraints": {},
  "craft_evidence": {},
  "impact_envelope": {},
  "risk_features": {},
  "risk_score": 9.6,
  "risk_factors": [],
  "triggered_rules": [],
  "policy_version": "v1",
  "nebius_model": "nvidia/nemotron-3-super-120b-a12b",
  "verdict": "BLOCK",
  "proof_status": "MISSING",
  "execution_status": "NOT_EXECUTED",
  "actual_impact": null
}
```

Pitch language:

> Complete audit trail.

Not:

> Immutable or tamper-evident audit trail.

---

# 17. POSTCONDITION VERIFICATION

After execution:

1. count actual deleted rows
2. compare actual count with the approved impact
3. verify production rows affected equals zero
4. record verification status
5. update workflow budget using the actual count

Success:

```text
Predicted: 92
Actual: 92
Production: 0
Status: VERIFIED
```

Failure:

```text
Actual count exceeds approved impact
OR production rows were affected
```

Then:

- restore snapshot if implemented
- record `ROLLED_BACK`
- otherwise record `MANUAL_REVIEW_REQUIRED`

The happy-path demo must show `VERIFIED`.

---

# 18. USER INTERFACE

Recommended: Streamlit.

## Required UI elements

### Header

```text
ProofGate
Dangerous actions need proof, not promises.
```

### Connection status

- CRAFT connected
- Nebius connected
- ProofGate ready
- Shadow CRM ready

### Controls

- `Run Unprotected`
- `Reset and Run Protected`
- user-instruction input
- visible label:
  `Simulating dropped environment constraint`

### Action trace

```text
CRAFT.get_schema                    SUCCESS
CRAFT.generate_sql                  SUCCESS
CRAFT.execute_query                 SUCCESS
Operations.preview_delete_users    10,073 affected
ProofGate.delete_users              BLOCK 9.6
Operations.create_snapshot          SUCCESS
ProofGate.delete_users              ALLOW 2.6
Postcondition.verify                VERIFIED
```

### BLOCK panel

```text
BLOCKED
Risk 9.6 / 10

9,981 production users would be deleted.
Missing constraint: environment=test
```

Below the headline:

```text
Triggered rules:
- RULE_INTENT_BOUNDARY
- RULE_RECOVERY_PROOF
- RULE_WORKFLOW_BUDGET
```

### Proof panel

```text
RECOVERY PROOF: VALID

Snapshot exists       ✓
Resource matches      ✓
Selector hash matches ✓
92 ≤ approved 92      ✓
```

### Closing metrics

```text
1 catastrophic action blocked
92 intended test users safely deleted
0 production users affected
92 / 100 workflow budget used
Complete audit trail
```

### Sanctioned fallback

A Rich terminal trace containing the same information.

Logic is more important than styling.

---

# 19. REPOSITORY STRUCTURE

```text
proofgate/
  README.md
  AGENTS.md
  CLAUDE.md
  .env.example
  pyproject.toml

  docs/
    ProofGate_PRD_FINAL_v4.md

  .claude/
    skills/
      proofgate-build-slice/
        SKILL.md
      proofgate-demo-check/
        SKILL.md

  app.py

  agent/
    orchestrator.py
    nebius_client.py
    prompts.py

  craft/
    client.py
    auth.py
    evidence.py

  proofgate/
    core.py
    policy.py
    impact.py
    proofs.py
    budgets.py
    audit.py
    models.py
    server.py

  operations/
    database.py
    seed.py
    actions.py
    pristine.db
    working.db

  scripts/
    smoke_demo.py

  tests/
    test_policy.py
    test_proof.py
    test_workflow.py
```

---

# 20. ENVIRONMENT VARIABLES

```bash
CRAFT_MCP_URL=https://nebius.emergence.ai/mcp
CRAFT_PROJECT_ID=<uuid>
CRAFT_OAUTH_CLIENT_ID=em-runtime-mcp

NEBIUS_API_KEY=<key>
NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com/v1/
NEBIUS_MODEL=nvidia/nemotron-3-super-120b-a12b

DEMO_DATABASE_PATH=operations/working.db
DEMO_PRISTINE_DATABASE_PATH=operations/pristine.db

PROOFGATE_POLICY_VERSION=v1
MAX_MUTATED_ROWS_PER_WORKFLOW=100
MAX_PRODUCTION_DELETIONS=0

DEMO_RELIABLE_MODE=true
DEMO_SIMULATE_SCOPE_DROP=true
```

---

# 21. BUILD PLAN — SOLO, APPROXIMATELY 5 HOURS

## 0:00–0:30 — Connectivity and skeleton

- create repo
- copy CRAFT starter authentication
- test CRAFT `hello_world`
- test one CRAFT metadata call
- test one Nebius structured JSON call
- create deterministic SQLite seed
- create minimal UI or terminal shell

Kill rule:

- CRAFT blocked after 25 minutes → ask support and continue
- prepare labeled cached fallback
- never stop local development

## 0:30–1:25 — Operations sandbox

Build:

- seed/reset
- preview
- direct hard delete
- snapshot creation
- delete
- before/after counts

Checkpoint:

- UNPROTECTED run deletes exactly 10,073
- reset restores exact initial state

## 1:25–2:30 — Enforcement core

Build:

- Pydantic contracts
- selector hashing
- Nebius intent extractor
- regex fallback
- Nebius risk extractor
- four deterministic policy rules
- real row budget
- JSONL audit

Checkpoint:

- bad broad action returns BLOCK
- no mutation occurs
- only actual rules are returned
- audit event is written

## 2:30–3:15 — Repair and proof

Build:

- machine-readable repair
- corrected `environment=test` proposal
- automatic snapshot
- proof validation
- retry same public `delete_users` tool
- ALLOW
- execute
- postcondition verification
- update row budget

Checkpoint:

```text
BLOCK → repair → PROOF VALID → ALLOW → 92/92/0 VERIFIED
```

## 3:15–3:50 — CRAFT evidence and UI

- execute one real CRAFT query flow
- show CRAFT evidence separately from Operations impact
- implement BLOCK panel
- implement populated proof panel
- implement final metrics
- implement the two-button demo

First complete demo must run by 3:50.

## 3:50–4:15 — Thin MCP adapter

Only if the full demo is green:

- expose preview
- expose snapshot
- expose guarded delete
- invoke one ProofGate tool through MCP

If FastMCP causes friction:

- keep the core working
- do not damage the demo

## 4:15–5:00 — Freeze and rehearse

- run full demo three times
- verify exact click sequence
- verify reset
- test Nebius fallback
- test CRAFT contingency
- write README
- freeze code after first clean rehearsal

No new architecture changes.

---

# 22. TWO-PERSON SPLIT

## Builder A — Runtime

Own:

- SQLite
- preflight
- snapshot
- proof validation
- policy
- budget
- execution
- postcondition
- audit
- MCP adapter

## Builder B — Integrations and UX

Own:

- CRAFT
- Nebius
- prompts and structured outputs
- agent orchestration
- repair loop
- Streamlit
- README
- pitch

Sync immediately on the Pydantic contracts and the guarded-versus-Operations boundary.

Do not let both builders redefine contracts independently.

---

# 23. ACCEPTANCE CRITERIA

## P0 — Submission bar

- [ ] Real Nebius structured intent extraction works
- [ ] Real Nebius structured risk extraction works
- [ ] Real CRAFT call succeeds, or fallback is explicitly labeled
- [ ] Exact operational blast radius is measured
- [ ] Broad action is truly blocked
- [ ] No protected mutation occurs after BLOCK
- [ ] Only actually triggered rules appear
- [ ] Agent repairs arguments automatically
- [ ] Snapshot proof is created
- [ ] Proof is bound to resource and selector
- [ ] Same public `delete_users` tool receives BLOCK then ALLOW
- [ ] Corrected action truly executes
- [ ] Postcondition shows 92/92/0 VERIFIED
- [ ] Workflow budget shows 92/100
- [ ] Audit JSONL is written
- [ ] Database resets
- [ ] Two runs work back-to-back
- [ ] Demo requires no more than three deliberate interactions

## P1 — Strong finalist bar

- [ ] ProofGate has a working MCP adapter
- [ ] Risk factors and triggered rules are displayed separately
- [ ] CRAFT evidence and Operations impact are clearly separated
- [ ] Nebius fallback works
- [ ] A second small call can be blocked by cumulative budget
- [ ] Demo completes in under 100 seconds

## P2 — Stretch

- [ ] HOLD tier
- [ ] human approval
- [ ] multiple action types
- [ ] hash-chain audit
- [ ] judge-entered live prompt
- [ ] policy configuration UI

---

# 24. FAILURE POLICY

## Nebius unavailable

- use deterministic extraction fallback
- consequential actions fail closed if intent is ambiguous

## CRAFT unavailable

- use clearly labeled previously retrieved evidence
- keep enforcement fully live
- explain the sponsor-environment issue honestly

## Impact preview unavailable

- trigger `RULE_UNKNOWN_IMPACT`
- block the consequential action

## Proof invalid

- block
- return the exact failed proof checks

## UI unavailable

- use Rich terminal trace

## Agent fails to generate the bad call

- deterministic `DEMO_SIMULATE_SCOPE_DROP=true`
- clearly label the simulated upstream bug

---

# 25. DEMO SCRIPT — APPROXIMATELY 95 SECONDS

## Opening — 10 seconds

> Access control can tell an agent whether it may call `delete_users`. It cannot tell whether this exact call deletes two test users or ten thousand production customers. ProofGate measures that blast radius before execution.

## Run 1 — 20 seconds

- click `Run Unprotected`
- show missing environment argument
- show 10,073 deleted
- highlight 9,981 production

> Authorized tool. Catastrophic arguments.

## Run 2 — 50 seconds

- click `Reset and Run Protected`
- show CRAFT enterprise evidence
- show operational preview
- show red `BLOCK 9.6`
- show triggered rules
- show automatic repair
- jump to populated `PROOF VALID` panel
- retry same public `delete_users`
- show `ALLOW`
- show `92 / 92 / 0 VERIFIED`

Do not pause to explain snapshot construction unless asked.

## Closing — 15 seconds

> ProofGate does not ask one LLM to approve another. Nebius extracts structured semantic risk, CRAFT supplies governed enterprise context, and deterministic policy controls execution. Dangerous actions need proof, not promises.

Show metrics.

---

# 26. JUDGE Q&A

## Why not RBAC?

RBAC determines whether an agent may call the tool. ProofGate determines whether this exact invocation exceeds the user’s intended blast radius.

## Why use an LLM inside a security product?

Nebius extracts semantic features such as missing constraints and intent mismatch. Normal Python rules make the final decision.

## Where exactly is Nebius used?

Twice:

1. extracting intended scope from the user instruction
2. extracting structured risk features by comparing intent, proposed arguments, and measured impact

It never issues the verdict.

## Why CRAFT?

CRAFT gives the agent governed access to enterprise schemas and read-only analytical evidence. It supplies the intelligence context preceding an operational action.

## Who calculates the exact 10,073 affected users?

The Operations preflight against the shadow CRM, because the system being mutated must be the authoritative source of exact blast radius.

## Why another MCP server?

CRAFT is intentionally read-only. Real agents combine intelligence tools with operational tools. ProofGate governs that action boundary.

## Is the destructive action fake?

It is a real mutation against a disposable SQLite shadow CRM. No actual customer system is touched.

## Why is proof necessary after scope is fixed?

Correct scope establishes alignment with intent. The snapshot proves an irreversible action is recoverable. Both must be true.

## Can a snapshot authorize a wrong production operation?

No. Proof is recoverability, not authorization. Intent mismatch remains a hard block.

## What stops 5,000 small calls?

ProofGate tracks the cumulative number of mutated rows across the workflow. Fragmentation does not bypass the authorized budget.

## Why only ALLOW and BLOCK?

For the MVP, BLOCK includes structured repair instructions. HOLD and human approval are natural extensions, but not needed to prove the runtime primitive.

## Why does the internal Operations delete not accept proof?

The mutation layer remains deliberately dumb and deterministic. Proof and policy belong at the enforcement boundary. Only the guarded ProofGate function may invoke the mutation in protected mode.

## Why does the UI show some reasons but not others?

Risk factors explain severity. Triggered policy rules explain enforcement. The UI renders only rules that actually fired.

## Is this truly MCP-native if the UI calls the core directly?

The core is transport-independent and has a thin MCP adapter. The UI uses the same core directly for demo reliability; production agents invoke it through MCP.

---

# 27. POSITIONING

## Name

ProofGate

## Subtitle

Blast-Radius Enforcement for AI Agents

## One-liner

> ProofGate measures the real-world impact of consequential agent actions and requires execution-bound safety evidence before they run.

## Problem

> Tool authorization is binary. Operational risk is continuous.

## Differentiator

> ProofGate does not merely ask whether an agent may use a tool. It checks whether the exact arguments match the user’s intended scope, measures what will actually be affected, and requires recovery proof bound to that precise action.

## Tagline

> Dangerous actions need proof, not promises.

## Closing thought

> The agent was authorized.  
> The action was still catastrophic.  
> ProofGate measured why—and stopped it.

---

# 28. FINAL PRIORITY ORDER

1. unsafe action truly blocked
2. corrected action truly executes
3. exact operational counts visible
4. same public tool gets different verdicts
5. rollback proof actually demonstrated
6. deterministic policy clearly visible
7. only actual triggered rules displayed
8. Nebius live and real
9. CRAFT live and real
10. postcondition VERIFIED
11. audit trail
12. demo reliability
13. UI polish
14. MCP wrapper
15. stretch features

A small complete product wins over a broad unstable prototype.
