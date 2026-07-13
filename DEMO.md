# ProofGate — Demo Runbook

## Startup command

```bash
source .venv/bin/activate
.venv/bin/streamlit run app.py
```

Opens at `http://localhost:8501`.

## Required environment setup

- Python virtual environment at `.venv/` with project dependencies installed
  (`pyproject.toml`): `pydantic`, `openai`, `python-dotenv`, `mcp`, `streamlit`.
- A `.env` file at the repo root with `NEBIUS_API_KEY` (optional — the app
  falls back to deterministic intent/risk extraction if absent or if the
  live call fails).
- No `CRAFT_PROJECT_ID` / CRAFT OAuth setup is required for the demo. The app
  forces `CRAFT_LIVE_ENABLED=false` at startup and always uses the committed,
  previously-retrieved evidence in `demo_seed/craft_evidence_seed.json`,
  auto-copied into the runtime cache (`artifacts/craft_evidence_cache.json`)
  on first launch if that cache file doesn't already exist.
- No login, no deployment, no external services required to demo locally.

## Page layout (Slice 15 redesign)

The page is organized around the actual governance pipeline, top to bottom:

```
Header + runtime status row (Operations DB · Deterministic policy · Audit · CRAFT · Nebius)
        ↓
User instruction
        ↓
CRAFT enterprise context (collapsed, subordinate — read-only, never decides)
        ↓
Proposed tool invocation  →  [Run unsafe agent action]  →  governance card:
        Nebius signals (subordinate) + Operations impact (intermediate)
        → missing requirements → Deterministic policy rules → VERDICT banner (dominant)
        → Recovery proof verification card (intermediate) → structured repair
        ↓
Corrected tool invocation →  [Apply verified repair]  →  same governance card:
        → VERDICT banner (dominant) → execution result → POSTCONDITION banner (dominant)
        ↓
Audit trail (collapsed — BLOCK event card + ALLOW event card)
```

This is a presentation-only reorganization: the **same** real backend calls,
buttons, and click order as before produce every value shown. The
deterministic-policy verdict banner (BLOCK/ALLOW) is the single largest,
most visually dominant element on the page; CRAFT and Nebius are compact,
muted, and collapsed/subordinate; Operations DB and Recovery Proof sit at an
intermediate visual weight — real authoritative inputs, but not the
decision itself.

## Reset procedure

**Always click "Reset demo" in the sidebar before presenting** — including
before every rehearsal and the actual presentation. This is the only
supported starting point; the app does not fully re-seed itself on a plain
page reload if the backend has already been mutated by an earlier session.

"Reset demo" does all of the following, in order:

1. Reseeds `operations/working.db` from `operations/pristine.db` (exact
   deterministic counts).
2. Zeroes the ProofGate workflow budget for this demo's workflow.
3. Clears the JSONL audit log (`artifacts/audit.jsonl`).
4. Clears all Streamlit session state (results, snapshot proof, errors).
5. Shows: **"Demo reset. Database reseeded and workflow state cleared."**

After reset, the corrected-action section is disabled again until the
unsafe action is run once — this is by design, not a bug. The interaction
order is unchanged: **Reset Demo → Run Unsafe Agent Action → Apply Verified
Repair**.

## Exact 2–3 minute click path

1. Click **"Reset demo"** (sidebar). Confirm the green success message.
2. Under **"Proposed tool invocation,"** click **"Run unsafe agent action"**.
   A governance card appears, in this order:
   - **Nebius** (subordinate, left column): the risk factors flagged for
     this call, as small code-style badges, plus the explanatory risk
     score.
   - **Operations DB** (intermediate, right column): Executed **No**,
     Total affected **10,073**, Production affected **9,981**, Test
     affected **92**.
   - A warning listing the **missing requirements** (includes
     `environment=test`) — this is the missing environment constraint.
   - **Deterministic policy**: the triggered rules
     (`RULE_INTENT_BOUNDARY`, `RULE_RECOVERY_PROOF`,
     `RULE_WORKFLOW_BUDGET`), immediately followed by the large **BLOCK**
     verdict banner.
   - **Recovery proof** verification card: validation status **MISSING**,
     "No rollback proof supplied," and the four check fields all showing
     an em dash (nothing to validate yet).
   - **Structured repair instructions** (JSON): the corrected call
     (`environment="test"`, `next_step: create_snapshot`).
3. Under **"Corrected tool invocation,"** read the callout — *"Same public
   tool `delete_users` — this call adds `environment=\"test\"` and a
   verified rollback proof"* — then click **"Apply verified repair"**.
   The same governance card renders again, this time:
   - Nebius / Operations DB reflect the corrected call (reduced blast
     radius: only the 92 test rows).
   - No triggered rules, followed by the large **ALLOW** verdict banner.
   - Recovery proof card: validation status **VALID**, real snapshot ID,
     protected resource `users`, maximum affected rows **92**, and all
     four checks marked **✓**.
   - **Execution result**: Test users deleted **92**, Production users
     deleted **0**, Workflow budget **92 / 100**.
   - The large **VERIFIED** postcondition banner.
   - (Optional) expand **"Technical details (raw proof)"** for the exact
     `snapshot_id` / `selector_hash` / `max_affected_rows` JSON.
4. (Optional, if a judge asks) Expand **"CRAFT enterprise context"** —
   shows label, cached mode, cohort question, generated SQL, result
   summary, tool trace, and the read-only/non-authoritative disclaimer.
5. (Optional) Expand **"Audit trail"** — shows both the BLOCK and ALLOW
   audit event cards with matching counts, side by side.

## Expected counts and verdicts (exact — unchanged by the redesign)

| Step | Verdict | Executed | Total | Production | Test | Budget | Postcondition |
|---|---|---|---|---|---|---|---|
| Unsafe (`environment=None`) | BLOCK | No | 10,073 | 9,981 | 92 | 0 / 100 | — |
| Corrected (`environment="test"`) | ALLOW | Yes | — | 0 | 92 | 92 / 100 | VERIFIED |

## Cached CRAFT behavior

`CRAFT_LIVE_ENABLED=false` is forced at the top of `app.py`. No OAuth flow
ever runs during the demo. The **"CRAFT enterprise context"** panel (near
the top of the page, just under "User instruction") always shows:

- Label: **"Previously retrieved CRAFT evidence"**
- Mode: **cached**
- The real cohort question, generated SQL, and result summary from an
  actual prior live CRAFT run (not fabricated)
- Tool trace (list of MCP tool calls that produced the cached result)
- Disclaimer: *"Read-only enterprise context — not authoritative for
  mutation impact"*

If `result_preview` is empty (as in the committed seed — the live run
returned a row but the bounded preview came back empty), only the
result summary text is shown; no empty raw preview is ever rendered.

## Warning: reset before every presentation

The Streamlit process keeps the operations database, workflow budget, and
audit log in shared, process-wide state (not per-browser-tab). If the app
has been used since it last started — by you, a teammate, or an earlier
rehearsal — **click "Reset demo" before presenting**, and use a single
browser tab. Opening a second tab against the same running process shares
the same backend state as the first.

## Emergency terminal fallback

If Streamlit itself won't start or the UI misbehaves, the entire flow can
be demonstrated directly from the terminal against the same real backend:

```bash
source .venv/bin/activate
.venv/bin/python -c "
from operations.database import reset_working_db
from operations.snapshots import create_snapshot
from proofgate.core import guarded_delete_users
from proofgate.budgets import reset_workflow_state, get_workflow_budget
from proofgate.audit import reset_audit_log
from proofgate.models import ActionContext

WORKFLOW_ID = 'demo-workflow'
INSTRUCTION = 'Clean up inactive test accounts that have not logged in for 90 days.'

reset_working_db()
reset_workflow_state(WORKFLOW_ID)
reset_audit_log()

ctx = ActionContext(workflow_id=WORKFLOW_ID, requesting_user='demo-judge', agent_id='demo-agent', original_instruction=INSTRUCTION)

unsafe = guarded_delete_users(action_context=ctx, inactive_days=90, environment=None, rollback_proof=None)
print('UNSAFE  verdict:', unsafe.verdict, 'executed:', unsafe.executed, 'risk:', round(unsafe.risk_score, 1))
print('        triggered:', [r.rule_id for r in unsafe.triggered_rules])

proof = create_snapshot(resource='users', inactive_days=90, environment='test', max_affected_rows=92)
corrected = guarded_delete_users(action_context=ctx, inactive_days=90, environment='test', rollback_proof=proof)
print('CORRECTED verdict:', corrected.verdict, 'executed:', corrected.executed)
print('budget:', get_workflow_budget(WORKFLOW_ID))
"
```

This prints the same BLOCK → repair → ALLOW result the UI shows, using the
identical `proofgate.core.guarded_delete_users` boundary — useful as a
narrated terminal fallback if the browser/Streamlit process is unavailable.

To confirm the test suite is healthy before presenting:

```bash
.venv/bin/pytest tests/ -q
```
