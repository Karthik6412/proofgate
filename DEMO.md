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
- A `.env` file at the repo root with `NEBIUS_API_KEY` (optional) and, if you
  want live CRAFT, `CRAFT_PROJECT_ID` (optional). Both are genuinely
  optional: see "Runtime modes" below for exactly what happens with or
  without them.
- No login, no deployment, no external services required to demo locally
  (`PROOFGATE_RUNTIME_MODE=fallback` or `=reliable_demo` guarantee this).

## Runtime modes (Slice 20)

ProofGate resolves one of three runtime modes per process, centralized in
`proofgate/runtime_mode.py`, used identically by both `app.py` and
`proofgate/mcp_server.py`:

| Mode | Behavior |
|---|---|
| `live` (**default**) | Prefer live Nebius and live CRAFT when configured; degrade honestly (never crash) on missing credentials or upstream failure. |
| `fallback` | Never attempt live Nebius or live CRAFT. Deterministic intent extraction + committed/cached CRAFT evidence only. Used automatically by the test suite. |
| `reliable_demo` | Same offline guarantee as `fallback`, intended for a judged/recorded demo where the exact click path and counts must never depend on network availability. |

Set it explicitly with:

```bash
PROOFGATE_RUNTIME_MODE=live streamlit run app.py
PROOFGATE_RUNTIME_MODE=fallback streamlit run app.py
PROOFGATE_RUNTIME_MODE=reliable_demo streamlit run app.py
```

or, for the MCP gateway:

```bash
PROOFGATE_RUNTIME_MODE=fallback python -m proofgate.mcp_server
```

With no explicit `PROOFGATE_RUNTIME_MODE`, mode resolves from the legacy
per-integration flags (`NEBIUS_LIVE_ENABLED`, `CRAFT_LIVE_ENABLED`,
`DEMO_RELIABLE_MODE`) if any are set — deprecated, logged once to stderr,
kept only for backward compatibility — else defaults to `live`.
Contradictory legacy flags (e.g. `NEBIUS_LIVE_ENABLED=true` with
`CRAFT_LIVE_ENABLED=false`, with no `PROOFGATE_RUNTIME_MODE` set) are
rejected with a clear error rather than guessed at.

**"Live" means live-preferred, not live-required.** `live` mode with no
`.env` configured at all runs fully offline with zero network calls —
missing credentials cause an immediate, silent-to-the-user degrade to
deterministic/cached behavior, logged once to stderr.

**CRAFT live retrieval is explicit and lazy (Slice 20.1):** the Streamlit
page's automatic evidence panel is populated on every page load (Streamlit
re-executes the whole script on each run), which for a fresh process is
functionally equivalent to "at import." CRAFT's OAuth flow opens a real
browser window and caches no token across processes, so — verified
empirically — attempting live CRAFT automatically from that call would
make a fresh `streamlit run app.py` attempt a real OAuth popup merely from
starting the app, if `CRAFT_PROJECT_ID` happens to be configured. That
automatic call therefore always passes `force_fallback=True`
(`craft.evidence.prepare_craft_evidence`'s new parameter) and is
*structurally* incapable of attempting live, in every runtime mode — not
merely configured not to.

> In live mode, ProofGate prefers live CRAFT evidence only after an
> explicit user request. Page load remains offline-safe and never
> initiates OAuth.

In `live` mode only, the "CRAFT enterprise context" panel shows one
explicit **"Fetch live CRAFT evidence"** button. Clicking it calls
`prepare_craft_evidence` without `force_fallback`, so it genuinely attempts
live CRAFT if configured. The result (live success, or fallback-after-
failure) is stored in session state, reused across reruns, and never
re-fetched automatically — the button disappears once clicked, and no
refresh action exists in this patch. `fallback`/`reliable_demo` modes
never show this button and never call live CRAFT. `Reset demo` clears this
state back to "not yet requested" (it does not and cannot revoke any
external OAuth token — those live only in the CRAFT client process's
memory, per `craft/auth.py`, never in Streamlit's session state).

Nebius has no equivalent import-time trigger (it only runs when you click
a governed action button) and needed no such restriction in Slice 20. See
`tests/test_app_import.py::test_importing_app_never_attempts_a_live_craft_call_even_with_real_env_credentials`
and `tests/test_lazy_craft_ui.py`.

**Exact CRAFT source labels** (`craft_evidence_source_label` in
`app_logic.py`):

- `Live` — a live fetch just succeeded.
- `Cached — live fetch not requested` — `live` mode, before the button is
  clicked.
- `Cached fallback — configuration unavailable` — fetch requested, but no
  usable `CRAFT_PROJECT_ID`/live configuration.
- `Cached fallback — live retrieval failed` — fetch requested and
  attempted, but failed (timeout, network error, invalid evidence).
- `Cached — fallback mode` — resolved mode is `fallback`.
- `Reliable demo evidence` — resolved mode is `reliable_demo`.
- `Unavailable` — no live evidence and no cache at all.

**MCP and CRAFT:** the MCP gateway (`proofgate/mcp_server.py`) never calls
CRAFT at all, in any mode — not even for fallback evidence. It never
imports any `craft.*` module. `guarded_execute`'s audit events for
MCP-driven calls always have `craft_evidence: null`, honestly reflecting
that CRAFT was never consulted. Explicit live CRAFT is therefore currently
a Streamlit-only capability; MCP's stdio process has no way to complete an
interactive browser OAuth flow, and this patch does not attempt to build
one.

**Test isolation:** `tests/conftest.py`'s autouse fixtures force
`PROOFGATE_RUNTIME_MODE=fallback` (plus the legacy flags, plus clearing
`NEBIUS_API_KEY`/`CRAFT_PROJECT_ID`) for every test. No test depends on a
developer remembering to export anything, and no test makes a real network
call regardless of what's in your own `.env`.

**Integration-source honesty:** every guarded call's audit event (and, for
delete/deactivate, the governance card in the UI, and the MCP response's
`extraction_mode` field) records the *actual* source for that call —
`nebius_live`, `mixed`, or `deterministic_fallback` — never a static
pre-call guess, and fallback output is never labeled live.

**Timeouts, retries, failure classes:** Nebius's OpenAI-compatible client
uses a 10s timeout; CRAFT's MCP client uses a 25s tool-call timeout and a
separate 180s OAuth-consent timeout (both pre-existing, unchanged). A
failed live attempt degrades once to deterministic/cached output — this
slice does not add automatic retries of the live call or of any
consequential mutation. Three failure classes are distinguished internally
(exposed via CRAFT's `error_summary` and Nebius's `degraded_reason`, an
internal field, not part of the audit schema): missing configuration
(no attempt made), upstream failure (attempted, network/API error), and
invalid response (attempted, received but failed validation).

**Secrets:** never logged, never returned in UI/MCP output. Both
integrations' error-summarization redacts anything that looks like an
`Authorization:`/`Bearer `/`api_key`/token value before it reaches a log
line, UI caption, or MCP response.

## Page layout (Slice 15 redesign, extended in Slice 18)

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
        ↓
"Same engine, a different tool" — deactivate_users second-tool demonstration
        (Slice 18): same governance card renderer, run through
        guarded_execute("deactivate_users", ...) instead of
        guarded_delete_users(...):
        Proposed tool invocation (reversible) → [Run reversible broad call]
        → governance card (BLOCK, RULE_INTENT_BOUNDARY + RULE_WORKFLOW_BUDGET only)
        → side-by-side rule comparison table (delete vs deactivate)
        → Corrected tool invocation (reversible) → [Apply reversible repair]
        → governance card (ALLOW, "Recovery proof: Not required for this
          reversible action", no snapshot) → execution result → POSTCONDITION
        → Deactivate audit trail (collapsed)
```

This is a presentation-only reorganization: the **same** real backend calls,
buttons, and click order as before produce every value shown. The
deterministic-policy verdict banner (BLOCK/ALLOW) is the single largest,
most visually dominant element on the page; CRAFT and Nebius are compact,
muted, and collapsed/subordinate; Operations DB and Recovery Proof sit at an
intermediate visual weight — real authoritative inputs, but not the
decision itself. The Slice 18 section below reuses this exact same
governance-card renderer for a second, reversible tool — it does not
introduce a second rendering system.

## Reset procedure

**Always click "Reset demo" in the sidebar before presenting** — including
before every rehearsal and the actual presentation. This is the only
supported starting point; the app does not fully re-seed itself on a plain
page reload if the backend has already been mutated by an earlier session.

"Reset demo" does all of the following, in order:

1. Reseeds `operations/working.db` from `operations/pristine.db` (exact
   deterministic counts) — this also restores any rows deactivated during
   the second-tool demonstration back to their seeded, non-deactivated state.
2. Zeroes the ProofGate workflow budget for **both** demo workflows:
   `demo-workflow` (delete) and `deactivate-demo-workflow` (deactivate).
3. Clears the JSONL audit log (`artifacts/audit.jsonl`) — shared by both
   demonstrations, so this clears both.
4. Clears all Streamlit session state (results, snapshot proof, errors, for
   both demonstrations).
5. Shows: **"Demo reset. Database reseeded and workflow state cleared."**

After reset, the corrected-action section is disabled again until the
unsafe action is run once — this is by design, not a bug. The interaction
order is unchanged: **Reset Demo → Run Unsafe Agent Action → Apply Verified
Repair**.

## Exact click path

**Important — click order across the two demonstrations:** both the delete
and deactivate demonstrations deliberately select the exact same 92 test
rows from the one shared `operations/working.db` (both use
`inactive_days=90, environment="test"`, to make the same-selector /
same-policy / different-reversibility comparison honest). `delete_users`
performs a real hard `DELETE`; `deactivate_users` only sets
`status='deactivated'` and `delete_users`'s selection predicate never
inspects `status`. So **"Apply reversible repair" must be clicked before
"Apply verified repair."** If "Apply verified repair" runs first, it
permanently removes those 92 rows and the deactivate demonstration will
show 0 affected instead of 92 (the app displays a warning if you do this
out of order — click "Reset demo" and redo the steps in the order below).
Each demonstration's own internal order (broad/unsafe → corrected/repair)
is unaffected; only the interleaving between the two sections matters.

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
3. Scroll down to **"Same engine, a different tool."** Under **"Proposed
   tool invocation (reversible),"** click **"Run reversible broad call"**.
   The same governance card renders for `deactivate_users`:
   - **Operations DB**: Executed **No**, Total affected **10,073**,
     Production affected **9,981**, Test affected **92** — identical counts
     to step 2, because both tools share the same selector.
   - **Deterministic policy**: exactly two triggered rules
     (`RULE_INTENT_BOUNDARY`, `RULE_WORKFLOW_BUDGET`) — **no**
     `RULE_RECOVERY_PROOF` — immediately followed by the large **BLOCK**
     verdict banner.
   - **Recovery proof**: *"Not required for this reversible action"*
     (not "MISSING" presented as an error) — a technical expander below
     still shows the raw `proof_status: MISSING` for anyone who wants it.
   - A compact **side-by-side rule comparison table** (delete vs
     deactivate) and the callout: *"Same rules, same guarded
     pipeline—different recovery requirement because deactivation is
     reversible."*
4. Under **"Corrected tool invocation (reversible),"** click **"Apply
   reversible repair"**. The governance card renders again:
   - No triggered rules, followed by the large **ALLOW** verdict banner.
   - Recovery proof: still *"Not required for this reversible action"* —
     no snapshot was created for this call.
   - **Execution result**: Test users deactivated **92**, Production
     users deactivated **0**, Workflow budget **92 / 100** (this
     demonstration's own separate `deactivate-demo-workflow` budget).
   - The large **VERIFIED** postcondition banner.
5. Now go back up and, under **"Corrected tool invocation,"** read the
   callout — *"Same public tool `delete_users` — this call adds
   `environment=\"test\"` and a verified rollback proof"* — then click
   **"Apply verified repair"**. The same governance card renders again:
   - Nebius / Operations DB reflect the corrected call (reduced blast
     radius: only the 92 test rows).
   - No triggered rules, followed by the large **ALLOW** verdict banner.
   - Recovery proof card: validation status **VALID**, real snapshot ID,
     protected resource `users`, maximum affected rows **92**, and all
     four checks marked **✓**.
   - **Execution result**: Test users deleted **92**, Production users
     deleted **0**, Workflow budget **92 / 100** (this demonstration's own
     separate `demo-workflow` budget).
   - The large **VERIFIED** postcondition banner.
   - (Optional) expand **"Technical details (raw proof)"** for the exact
     `snapshot_id` / `selector_hash` / `max_affected_rows` JSON.
6. (Optional, if a judge asks) Expand **"CRAFT enterprise context"** —
   shows label, cached mode, cohort question, generated SQL, result
   summary, tool trace, and the read-only/non-authoritative disclaimer.
7. (Optional) Expand **"Audit trail"** — shows both the BLOCK and ALLOW
   audit event cards for the delete demonstration, side by side. Expand
   **"Deactivate audit trail"** for the same, for the deactivate
   demonstration.

## Expected counts and verdicts (exact — unchanged by the redesign)

| Step | Verdict | Executed | Total | Production | Test | Budget | Postcondition |
|---|---|---|---|---|---|---|---|
| Unsafe (`environment=None`) | BLOCK | No | 10,073 | 9,981 | 92 | 0 / 100 | — |
| Corrected (`environment="test"`) | ALLOW | Yes | — | 0 | 92 | 92 / 100 | VERIFIED |

## Same engine, a different tool: deactivate_users (Slice 18)

`deactivate_users` is registered through the same `guarded_execute(...)`
pipeline as `delete_users`, and evaluated by the exact same four
deterministic policy rules — the different outcome below comes entirely
from `deactivate_users` being a reversible action (`hard_delete=False`),
not from any tool-specific branch in the policy engine.

### Broad reversible call (`environment=None`)

- Verdict: **BLOCK**
- Total affected: **10,073**, Production affected: **9,981**, Test
  affected: **92** — identical to the broad delete call (same selector).
- Triggered rules exactly:
  - `RULE_INTENT_BOUNDARY`
  - `RULE_WORKFLOW_BUDGET`
- `RULE_RECOVERY_PROOF` does **not** trigger, because the action is
  reversible.

### Corrected reversible call (`environment="test"`)

- Verdict: **ALLOW**
- Test affected: **92**, Production affected: **0**
- No rollback proof is required or supplied, and no snapshot is created.
- Postcondition: **VERIFIED**
- Workflow budget: **92 / 100** — on its own separate
  `deactivate-demo-workflow` identity, isolated from the delete
  demonstration's `demo-workflow` budget so the two 92-row mutations never
  combine into one 184-row total.

Do not change the existing delete demonstration's expected values above —
they remain exactly as they were before this slice.

## Cached CRAFT behavior

The Streamlit page's *automatic* CRAFT evidence panel always uses
cached/fallback evidence on page load, in every runtime mode (see
"Runtime modes" above for why) — no OAuth flow ever runs from merely
opening the page. Before clicking "Fetch live CRAFT evidence" (`live`
mode only; see "Runtime modes"), the **"CRAFT enterprise context"** panel
(near the top of the page, just under "User instruction") always shows:

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

## MCP guarded gateway (Slice 19)

ProofGate can also run as an MCP server, so an external MCP-compatible
agent client -- not just this Streamlit app -- can call the guarded
`delete_users` and `deactivate_users` actions.

> The current MCP gateway exposes the registered `delete_users` and
> `deactivate_users` actions through ProofGate's shared guarded execution
> boundary.

It does **not** automatically govern arbitrary MCP tools, and Operations
functions (preview, mutation, snapshot creation, database/audit reset)
are never exposed as public MCP tools -- only the two guarded actions
above are.

### Package, transport, and startup

- Package: `mcp` (already an approved dependency), version **1.28.1**.
- Transport: **stdio** (the SDK's low-level `mcp.server.lowlevel.Server`,
  not `FastMCP`'s decorator sugar -- see `proofgate/mcp_server.py`'s module
  docstring for why).
- Start it with:

  ```bash
  source .venv/bin/activate
  NEBIUS_LIVE_ENABLED=false CRAFT_LIVE_ENABLED=false python -m proofgate.mcp_server
  ```

- Importing `proofgate.mcp_server` never starts a server -- it only
  constructs the `Server` object and registers handlers (plus one cheap
  startup assertion that the registry still contains both expected
  tools). The stdio loop only begins under `if __name__ == "__main__":`.

### Minimal request shape

Both tools accept the same flat JSON shape:

```json
{
  "instruction": "Clean up inactive test accounts that have not logged in for 90 days.",
  "workflow_id": "mcp-run-001",
  "inactive_days": 90,
  "environment": null,
  "rollback_proof": null
}
```

`rollback_proof`, when supplied for `delete_users`, uses the exact same
`RollbackProof` shape as everywhere else in ProofGate (`snapshot_id`,
`resource`, `selector_hash`, `max_affected_rows`) -- the server
cross-validates it against real server-side snapshot metadata exactly as
the existing `validate_rollback_proof` already does; it never trusts the
client's claim alone. `deactivate_users` never requires a proof.

### Expected behavior

- Broad `delete_users` (`environment=null`): structured `BLOCK`, `10,073`
  total / `9,981` production / `92` test, triggered rules exactly
  `RULE_INTENT_BOUNDARY`, `RULE_RECOVERY_PROOF`, `RULE_WORKFLOW_BUDGET`.
- Broad `deactivate_users` (`environment=null`): structured `BLOCK`, same
  counts, triggered rules exactly `RULE_INTENT_BOUNDARY`,
  `RULE_WORKFLOW_BUDGET` (no `RULE_RECOVERY_PROOF`).
- **A policy `BLOCK` is returned as normal structured MCP tool output**,
  not a transport-level error. Only malformed input, an unregistered tool
  name, or an internal failure produce an MCP tool error.
- A valid call that reaches ProofGate's guarded pipeline writes **exactly
  one** normal ProofGate audit event, through the same shared
  `artifacts/audit.jsonl` path and schema used by Streamlit and direct
  Python calls.
- An invalid, schema-level request (unknown field, wrong type, missing
  required field) never reaches the guarded pipeline and writes **zero**
  audit events.

### Input normalization (narrow and tested)

Allowed: trimming surrounding whitespace on `instruction`/`workflow_id`;
`"TEST"`/`"PRODUCTION"` (any case) normalized to `"test"`/`"production"`;
a strictly numeric string such as `"90"` accepted for `inactive_days`.

Rejected before the guarded pipeline ever runs: unknown/misspelled fields
(e.g. `enviroment`), non-numeric strings (`"ninety"`), negative or zero
`inactive_days`, floating-point thresholds, unsupported environment
aliases (e.g. `"testing"`), and any omitted required field.

### Runtime mode (Slice 20)

The MCP gateway resolves and applies the same centralized runtime mode as
Streamlit (`proofgate.runtime_mode.apply_runtime_mode_to_environment()`,
called once at import time -- an environment-variable resolution only, no
network call). In `live` mode, a guarded call's Nebius extraction may be
attempted live and degrades gracefully on failure, exactly as it does for
Streamlit; the MCP gateway never calls CRAFT at all (CRAFT integration is
out of scope for MCP tool handlers), so there is no equivalent CRAFT
concern here. The resolved mode is logged once to stderr at startup. The
per-call response additionally includes `extraction_mode` and
`nebius_model` -- additive, backward-compatible fields already present on
the audit event this call wrote; BLOCK and ALLOW responses still share one
identical key set.

### Concurrency, queueing, and shutdown

- The tool handler is `async def` (required by the installed SDK), but
  calls the existing synchronous `guarded_execute(...)` with no
  surrounding `await`. Because the SDK dispatches on a single-threaded
  event loop, a synchronous call cannot be preempted mid-execution --
  every consequential call in one server process is therefore processed
  **strictly serially**, with **no explicit lock, semaphore, or queue**.
  This is proven deterministically (not by timing) in
  `tests/test_mcp_server.py`'s concurrency test.
- Because no explicit lock/queue exists, there is no queue-wait timeout
  to configure or test -- this is stated explicitly rather than silently
  omitted.
- This serialization guarantee is scoped to one server process. Running
  this gateway and the Streamlit app against the same
  `operations/working.db` at the same time is not covered by this
  guarantee and is out of scope for this slice.
- `SIGTERM`/`SIGINT` set a shutdown flag (the raw signal handler does
  nothing beyond that plus one log line); once set, new consequential
  calls are rejected with a safe structured error before any guarded work
  begins. An already-in-flight call is not interrupted -- it cannot be,
  short of killing the process. This gateway does not claim protection
  from `SIGKILL`, power loss, or abrupt termination.

### Logging

All server-side logs go to **stderr** (`proofgate.mcp_server`'s own
logger, plus the SDK's own default). Stdout is reserved exclusively for
MCP protocol frames; the server never calls `print()`. Verified directly:
a raw subprocess run with no client attached produced exactly 0 bytes on
stdout.

### Local-development-only limitations

This is a local, single-process, unauthenticated stdio server for
development and demonstration. It has no network authentication, no
deployment infrastructure, no multi-tenant isolation, and no crash/restart
supervisor. Transaction boundaries in `operations/actions.py` (explicit
`BEGIN IMMEDIATE` + commit/rollback + scoped connections, already in place
before this slice) are unchanged and were only inspected, not rewritten,
for this slice.

Run the MCP-specific tests directly with:

```bash
.venv/bin/pytest tests/test_mcp_server.py -q
```
