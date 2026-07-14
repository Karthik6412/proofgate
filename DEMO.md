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

## Real agent loop against the real MCP gateway (Slice 21)

`scripts/agent_loop.py` is a standalone, live-only experiment proving that
an independent agent -- not a human- or test-authored call -- can discover
ProofGate's real MCP tools, choose one, propose its business arguments,
and have the real guarded pipeline evaluate the resulting request.

> The model chooses a discovered MCP tool and proposes only its business
> arguments. The trusted client supplies the original instruction, a
> unique workflow ID, and no rollback proof before submitting the request
> through the real MCP gateway.

The model does **not** control ProofGate metadata: it never sees or sets
`workflow_id`, `rollback_proof`, `requesting_user`, `agent_id`, or any
policy/verdict/audit field. Its entire contribution is a `tool_name` plus
`inactive_days`/`environment` -- both `AgentToolProposal` and
`ProposedMutationArguments` (in `agent/agent_proposal.py`) use
`extra="forbid"`, so any attempt to smuggle other fields in is rejected
before anything is trusted.

### Startup

```bash
source .venv/bin/activate
PROOFGATE_RUNTIME_MODE=live python scripts/agent_loop.py --runs 5
```

Requires `PROOFGATE_RUNTIME_MODE=live` and a real, configured
`NEBIUS_API_KEY` (`NEBIUS_LIVE_ENABLED` not explicitly disabled). If either
is missing, the script exits immediately (before any proposal generation
or MCP call) with a concise message -- it never silently substitutes
deterministic fallback and calls the result "live."

### What happens each run

1. Reset `operations/working.db`, the workflow-budget state (via a fresh,
   never-reused `agent-loop-<run>-<uuid>` workflow ID), and the enforcement
   audit log; verify the deterministic `10,623`-row total.
2. Ask the real Nebius-hosted model to choose one tool from the **real,
   live-discovered** MCP tool list (`session.list_tools()` -- never a
   hardcoded duplicate) and propose that tool's `inactive_days`/
   `environment`. At most one provider request; any timeout, error,
   malformed output, undiscovered tool name, or invalid argument becomes
   `PROPOSAL_FAILURE` -- never guessed, never repaired, never submitted.
3. For a valid proposal, the script -- never the model -- constructs the
   final request: the unchanged original instruction, the fresh
   `workflow_id`, the proposal's `inactive_days`/`environment` copied
   through unchanged, and `rollback_proof: null`.
4. Submits it through the real MCP stdio transport (the same
   `mcp.client.stdio.stdio_client` + `mcp.ClientSession` path proven in
   Slice 19's protocol-integrity test) and records the real structured
   ProofGate response.

### First-attempt proof semantics

Every submitted request has `rollback_proof: null`. A correctly-scoped
`delete_users` proposal (`environment="test"`) is therefore still expected
to `BLOCK` on `RULE_RECOVERY_PROOF` alone -- that is a correctly-scoped
irreversible proposal lacking required recovery proof, **not** a
missing-scope mistake, and the transcript/report distinguish the two
explicitly (`recovery_proof_only_block` vs `missing_filter_mistake`). A
correctly-scoped `deactivate_users` proposal may reach `ALLOW`, since
deactivation is reversible -- this is retained and reported honestly, not
hidden.

### Retained outcomes and aggregate metrics

Both safe and unsafe proposals are retained; the script never cherry-picks
runs. After all runs it reports the complete distribution: proposal
status, tool selection, scope classification, `BLOCK`/`ALLOW` counts,
per-rule trigger counts, execution counts, `missing_filter_mistake_count`,
`recovery_proof_only_block_count`, `combined_scope_and_proof_block_count`,
and `production_rows_mutated_total` (required to be exactly `0` across
every run; the script stops and reports prominently, without
characterizing the experiment as successful, if this is ever violated).

### Transcript artifact

One sanitized JSON transcript per experiment, written to
`artifacts/agent_loop_<timestamp>.json` (the existing gitignored artifact
directory -- never committed automatically). Contains the discovered
tools, every run's validated proposal, the trusted metadata the script
added, the real MCP result, per-run classification, and aggregate
metrics. Never contains API keys, tokens, raw provider objects,
authorization headers, or `.env` contents.

### Offline test behavior

`tests/test_agent_proposal.py` and `tests/test_agent_loop_script.py`
exercise every importable helper (proposal validation, trusted-request
construction, run classification, aggregate metrics, transcript assembly,
CLI gating) using fake/mocked providers and subprocess-level gating checks
only -- pytest never calls the real Nebius API or spawns a real live MCP
experiment.

### Known limitations

- No retry loop exists in this slice -- a `PROPOSAL_FAILURE` run is
  recorded and skipped, never retried automatically. (Slice 22, below,
  adds one opt-in bounded repair *attempt* after an eligible `BLOCK` --
  still not a loop, and still never retried.)
- Real model nondeterminism and provider latency/rate-limit behavior are
  not characterized beyond a small local sample.
- Explicit live CRAFT (Slice 20.1) is unrelated to and untouched by this
  experiment; the agent-loop script never calls CRAFT.
- `Streamlit` (`app.py`/`app_logic.py`) and reliable-demo behavior are
  completely unchanged by this slice.

### Reset behavior

Every run resets the database, the (fresh, per-run) workflow budget, and
the audit log before it starts. After the full experiment, the script
resets the database and audit log again, leaving the repository in clean
deterministic runtime state regardless of how many runs were requested or
how they were classified.

## One bounded repair attempt (Slice 22)

`--allow-repair` is an opt-in flag on the same script. **Default off means
Slice 21 behavior exactly** -- no repair prompt is built, no second
provider request is made, and at most one MCP submission occurs per
workflow, identically to Slice 21.

```bash
source .venv/bin/activate
PROOFGATE_RUNTIME_MODE=live python scripts/agent_loop.py --runs 5 --allow-repair
```

### What a repair attempt is

After the real, unchanged initial proposal is submitted through the real
MCP gateway exactly as in Slice 21, if (and only if) every one of these
holds:

- the initial proposal was valid and was actually submitted,
- MCP transport succeeded,
- the verdict was `BLOCK`,
- `executed` was `false`,
- ProofGate's own response included a non-empty `suggested_repairs` list,
- repair has not already been attempted for this workflow, and
- `--allow-repair` was passed,

...the script makes **exactly one** additional provider request
(`agent/agent_repair.py::propose_repair`), asking the same live model to
revise its tool choice and/or business arguments in light of ProofGate's
own real enforcement feedback, then submits **exactly one** additional
request through the same real MCP gateway. A second `BLOCK` ends the
workflow -- there is no second repair attempt, no retry of the repair
model call, and no retry of the repair MCP submission.

If the initial `BLOCK` ever reports `executed=true`, this is treated as a
hard safety failure: the run stops immediately, state is reset, and it is
reported prominently rather than treated as an ordinary ineligible case.

### What the repair model sees (and does not see)

The repair model receives: the unchanged original instruction, its own
original validated proposal, the actual verdict, the actual triggered
rule identifiers, and ProofGate's exact `suggested_repairs` list --
**passed through completely unchanged**, never rewritten, summarized, or
reinterpreted -- plus the same live-discovered tool names/descriptions/
schemas used for the initial proposal.

It does **not** see: the full MCP response, audit metadata, the workflow
ID, raw budget internals beyond what `suggested_repairs` already
contains, database paths, selector hashes, snapshot paths, proof
payloads, API keys, `.env` values, or any instruction telling it which
tool or `environment` value to pick. It is asked only for "one revised
tool choice and business argument set that responds to the supplied
enforcement feedback," plus an optional short explanation.

`agent/agent_repair.py`'s `AgentRepairProposal` and `SanitizedRepairFeedback`
both use `extra="forbid"`, so any attempt by the model to also emit
`rollback_proof`, `workflow_id`, `instruction`, proof data, a verdict, or
any other control-plane field -- top-level or nested -- is rejected by
construction, exactly like Slice 21's `AgentToolProposal`.

### No proof creation or attachment

> Slice 22 does not create, attach, or repair rollback proof. Both
> initial and repaired submissions use `rollback_proof=null`. The agent
> may respond to recovery guidance by selecting a reversible tool, but it
> cannot satisfy recovery-proof requirements by inventing evidence.

> The repair model may revise the selected tool and business arguments
> once. The revised request is reevaluated through the same real MCP
> gateway and the same deterministic ProofGate policy path.

A repaired `deactivate_users` proposal may `ALLOW` because deactivation is
reversible -- not because any proof was supplied. A repaired `delete_users`
proposal, even correctly scoped to `environment="test"`, remains `BLOCK`ed
by `RULE_RECOVERY_PROOF` alone, because no proof exists on either attempt.

### Same workflow ID, same audit log, same budget

The initial and repair attempts share one workflow ID. This is safe and
intentional, verified directly against the actual implementation before
Slice 22 was built (not assumed): `proofgate/audit.py` enforces no
uniqueness constraint on `workflow_id` (each event gets its own globally
unique `event_id`, and the MCP server's own audit lookup matches on
`event_id`, never `workflow_id`), and `proofgate/budgets.py::record_execution`
is the only function that increments workflow budget, called only on
`ALLOW` -- so a `BLOCK` (initial or repair) always consumes zero budget,
and an `ALLOW`ed repair consumes budget exactly once. A real live run
confirms this empirically: both the initial and repair audit events for
each workflow share the same `workflow_id`, appear in submission order,
and each one's own response reflects only that submission's own data.

### Repair classification

Each workflow reports:

- `repair_status`: `NOT_ATTEMPTED | PROPOSAL_FAILURE | VALID_REPAIR |
  MCP_VALIDATION_FAILURE | TRANSPORT_FAILURE`
- `repair_change` (comparing the validated initial and repair proposals):
  `unchanged | tool_changed | scope_changed | threshold_changed |
  multiple_fields_changed`
- `repair_effect` (comparing the initial and repair triggered-rule sets):
  `resolved_all_rules | resolved_some_rules | resolved_no_rules |
  introduced_new_rules | proposal_failed | not_attempted`
- `repair_success`: only `true` when the repair was actually submitted,
  `ALLOW`ed, mutated zero production rows, and reached a `VERIFIED`
  postcondition -- a `BLOCK`ed repair (e.g. a repaired `delete_users`
  still lacking proof) is reported honestly as an unsuccessful repair
  safely rejected by ProofGate, never as a system failure.

### Boundedness (structural, not just disciplined)

- At most one repair-model provider request per workflow
  (`propose_repair` has exactly one call site in `scripts/agent_loop.py`).
- At most two MCP submissions per workflow (`_submit_via_mcp` has exactly
  two call sites: initial, repair).
- No `while` loop and no recursive repair call in `_run_experiment`.
- A second `BLOCK` (on the repaired attempt) ends the workflow; there is
  no second eligibility check and no second repair attempt.

### Offline test behavior

`tests/test_agent_repair.py` covers repair-proposal validation (valid
unchanged/tool-switch/scope-change/threshold-change/multi-field repairs;
rejection of every control-plane field, top-level and nested) and
`propose_repair`'s success/failure paths using a fake provider client --
no real network. `tests/test_agent_loop_script.py` covers repair
eligibility (every combination), change/effect/success classification,
workflow-summary assembly, aggregate metrics with and without repair, the
structural boundedness checks above, and CLI regression confirming
`--allow-repair`'s default-off behavior is textually and behaviorally
identical to Slice 21.

### Streamlit and reliable-demo: unchanged

`app.py`, `app_logic.py`, and the `RELIABLE_DEMO` runtime mode are
completely untouched by this slice -- confirmed by `git diff --stat --
app.py app_logic.py` reporting no changes.

## Real automatic rollback (Slice 23)

> ProofGate first verifies that a recovery artifact exists and is bound to
> the exact irreversible action. Slice 23 then demonstrates that the same
> verified artifact can actually restore the sandboxed database after a
> genuine postcondition mismatch.

Prior slices proved a valid recovery proof is *required* before an
irreversible `delete_users` call can be `ALLOW`ed. They never proved the
underlying snapshot could actually restore anything. Slice 23 closes that
gap with a real, trusted restoration operation, wired automatically into
the existing post-execution pipeline -- no new MCP tool, no agent-facing
rollback action, no policy/proof/selector-hash/audit-schema changes.

### The real sequence

```text
Verified recovery artifact
-> irreversible action ALLOWed
-> real mutation executes
-> normal postcondition verification detects a genuine mismatch
-> trusted restoration runs
-> independent restoration verification runs
-> ROLLED_BACK or MANUAL_REVIEW_REQUIRED
```

### Three distinct steps -- never conflated

- **Recovery proof validation** (`proofgate/proofs.py`, unchanged): before
  execution, checks the caller-supplied `RollbackProof` against the
  server-side snapshot metadata -- existence, resource, selector hash,
  approved row count. This is what makes the original `ALLOW` possible.
- **Real restoration** (`operations/restoration.py::restore_snapshot`,
  new): after a genuine mismatch, re-validates the same snapshot (its own
  integrity and selector binding, independently, a second time) and
  transactionally syncs the snapshot's `environment='test'` rows back
  into `operations/working.db`. Never touches `operations/pristine.db`,
  never restores or inserts a production row, never partially commits --
  any conflicting existing row aborts the whole transaction.
- **Independent post-restoration verification**
  (`proofgate/rollback.py::resolve_final_postcondition`, new): does not
  trust `restore_snapshot`'s own self-report. It separately recomputes a
  row-content digest and row counts directly from the snapshot artifact
  (the authoritative pre-execution state) and compares them against
  `operations/working.db`'s post-restoration state. Only when that
  independent comparison agrees does it report `ROLLED_BACK`.

### Automatic rollback eligibility

> Automatic rollback is attempted only after an executed ALLOW for a
> rollback-capable action with a previously validated recovery artifact.

Concretely, all of the following must hold:

- the verdict was `ALLOW` and the mutation actually executed
- the tool is rollback-capable (`hard_delete=True` in the registry --
  currently only `delete_users`; `deactivate_users` is reversible and
  never carries a recovery proof, so it is never eligible)
- a valid recovery proof was verified *before* execution
- the exact validated snapshot identifier is still available
- normal, unchanged `postcondition.verify_postcondition` returned
  `MISMATCH`

A `MISMATCH` with no eligible recovery artifact becomes
`MANUAL_REVIEW_REQUIRED` directly -- no guessed or unrelated restore is
ever attempted. A `MANUAL_REVIEW_REQUIRED` caused by genuine production
impact is left as-is (restoration here can never touch a production row
by construction, so it could never honestly resolve that case).

### The mismatch demonstration flag

```bash
DEMO_SIMULATE_POSTCONDITION_MISMATCH=true
```

> The deterministic mismatch mechanism performs a real mutation against
> the local working database. It does not fabricate a postcondition
> result.

Off by default. When set, `operations.actions.delete_users` -- after
completing the caller's own real, predicate-matched deletion -- deletes
exactly one additional, real test-environment row the caller's selector
did not target, and honestly folds that real count into the
`MutationResult` it returns. `postcondition.verify_postcondition` is
completely unmodified: it detects the resulting mismatch purely because
the actual affected count genuinely differs from the predicted count, the
same way it always has. The flag can never target
`operations/pristine.db`, can never touch a production row, and never
assigns or mocks `PostconditionResult` directly.

**Why the mismatch can't be a smaller number than reality:** because
`verify_postcondition` only ever compares the predicted count against the
mutation's own honestly-reported actual count, a *genuine* mismatch
necessarily means the real total affected count differs from the
originally-predicted 92. In this demo that real total is 93 (92 intended
+ 1 demo-injected). Workflow budget honestly reflects that real total
(93/100) -- rollback itself still adds nothing beyond that and refunds
nothing. The general, unrelated corrected-delete invariant of 92/100
(Slices 7-9, exercised without this flag) is unaffected and still holds.

### Budget behavior

> Rollback does not refund workflow budget. The original consequential
> execution remains charged because it occurred, even when its database
> effects are later reversed.

`proofgate/budgets.py::record_execution` is untouched. It is called
exactly once per `guarded_execute` invocation, keyed only on the real
`MutationResult` the mutation itself returned -- never on
`PostconditionResult`. Automatic rollback resolves the *status* value
passed into that one call, not the mutation counts, so:

- a `BLOCK`ed request never consumes budget (unchanged)
- an `ALLOW`ed, rolled-back execution consumes budget exactly once, equal
  to the real total mutation (92 in the general case; 93 when the demo
  flag injected one extra real row)
- rollback itself consumes zero *additional* budget and refunds zero
- calling `operations.restoration.restore_snapshot` a second time
  (idempotent no-op) never touches budget at all -- it is a plain
  Operations function with no budget awareness

### Audit behavior

No audit schema change. `proofgate/core.py::guarded_execute` already
computes `postcondition_result` and builds/appends its one `AuditEvent`
in the same synchronous call; Slice 23 only inserts the rollback
resolution *between* those two existing steps, so the single audit event
this call already wrote now honestly carries `ROLLED_BACK` or
`MANUAL_REVIEW_REQUIRED` in its existing `postcondition_result` field.
Exactly one enforcement audit event is produced per real MCP submission,
exactly as before; rollback never appends a second, fake enforcement
event and never rewrites history.

### Idempotency and conflicts

A second call to `restore_snapshot` with the same snapshot is a safe
no-op (`ALREADY_RESTORED`): no duplicate rows, no changed rows, no
budget, no audit event. If an existing row's primary key is present but
its content genuinely conflicts with the snapshot's copy, the entire
restoration transaction is rejected (`REJECTED_CONFLICT`) and rolled
back -- nothing is partially restored, nothing unrelated is overwritten.

### Running the demonstration

```bash
source .venv/bin/activate
python scripts/rollback_demo.py
```

Requires only the local sandbox: no live Nebius, no CRAFT, no
arguments. The MCP subprocess's own runtime mode is explicitly forced to
`fallback` in its environment (never `live`), and the mismatch flag is
enabled only inside that same subprocess environment. The script resets
the database/audit/workflow state before starting, submits a real
corrected `delete_users` through the real MCP stdio gateway with a real
recovery proof, verifies the genuine mismatch/restoration/independent-
verification sequence end to end, exercises the trusted restore
function's idempotency a second time, writes a sanitized transcript to
`artifacts/rollback_demo_<timestamp>.json` (gitignored, contains no
snapshot paths, database paths, proof payloads, secrets, or raw
exception traces), and resets the sandbox again after capturing evidence.

### Unaffected by this slice

`app.py`/`app_logic.py`/`RELIABLE_DEMO`, `scripts/agent_loop.py`'s agent
proposal and repair behavior, the MCP request/response contracts, the
deterministic policy engine, proof-validation semantics, canonical
selector hashing, and the tool registry are all completely unchanged.
Rollback is not, and does not add, an agent-facing MCP tool -- it is
reachable only through the existing guarded post-execution pipeline.

## A non-user-management consequential tool: set_feature_flag (Slice 25)

> `set_feature_flag` demonstrates that ProofGate's guarded boundary is not
> tied to SQL deletion or user-management selectors. The same
> deterministic policy, impact, budget, postcondition, audit, repair, and
> MCP pipeline governs a filesystem-backed configuration mutation.

### Why this tool, and why not outbound notifications

A third tool was added specifically to prove genericity across a
*resource*, not just across reversibility (Slice 18 already proved that
across two tools on the same `users` table). Simulated outbound
notification delivery was considered and rejected for this slice: it is
inherently irreversible in a way that would force redefining recovery
proof as audience approval rather than recoverability, adding a new proof
type, adding a human-approval concept, or permanently blocking the
corrected case -- any of which would broaden this slice into a
proof-model redesign rather than a genericity proof. `set_feature_flag`
stays reversible under the *existing* proof semantics, so it tests
architectural genericity in isolation.

### How the resource differs from `users`

|                | `delete_users` / `deactivate_users` | `set_feature_flag` |
|----------------|--------------------------------------|---------------------|
| Backend        | `operations/working.db` (SQLite)     | `operations/feature_flags_working.json` (local JSON) |
| Selector shape | `inactive_days`, `environment`       | `flag_name`, `enabled`, `environment`, `rollout_percentage` |
| Mutation       | row deletion / status update         | atomic configuration replacement |
| Reversibility  | irreversible / reversible            | reversible |

`operations/feature_flags.py` never imports `operations.database` or
`operations.actions`, and never touches the users table -- confirmed by
a dedicated AST-based test. Audience counts (`operations/
feature_flag_audience.json`: `test=92`, `production=9981`, `total=10073`)
are fixed, deterministic local data, never derived by querying the users
table.

> Feature-flag impact is calculated by deterministic code from registered
> audience data. No language model determines the affected-user count.

### Impact rounding: effective-exposure accounting (corrected in Slice 25.1)

> Feature-flag blast radius is the number of users whose effective feature
> exposure changes, not simply the audience implied by the requested
> rollout percentage.

Slice 25's original formula (`affected = audience * requested_rollout //
100` whenever the request differed from current state) worked for
enabling a flag from off, but was wrong for disabling one: a request
that fully disabled a flag previously at 100% rollout reported `0`
affected users, when the true answer is that all previously-exposed
users lose access. Slice 25.1 corrected this by comparing *effective
exposure* before and after the request, for each targeted environment:

```python
current_effective_percentage = current_rollout_percentage if current_enabled else 0
requested_effective_percentage = requested_rollout_percentage if requested_enabled else 0
current_exposed_users = audience * current_effective_percentage // 100
requested_exposed_users = audience * requested_effective_percentage // 100
affected_users = abs(requested_exposed_users - current_exposed_users)
```

(Integer floor rounding throughout, exactly as before.)

> Preview and execution use the same deterministic environment-change
> plan, so enabling, disabling, increasing rollout, and decreasing
> rollout are accounted for symmetrically.

Both `preview_set_feature_flag` and `set_feature_flag` derive their
numbers from one shared `EnvironmentChangePlan` (`operations/
feature_flags.py::_plan_environment_change`) -- neither computes impact
independently, so they can never disagree.

**Configuration changed is a different question from user exposure
changed.** A request whose stored `rollout_percentage` differs from the
current value while the flag is disabled both before and after (e.g.
disabled at a stored 80% moving to disabled at 0%) still updates the
authoritative file and increments version, but honestly reports `0`
affected users -- no user's *effective* exposure changed. An exact
no-op (requested state byte-identical to current state) performs no
file write at all, not merely a write that reproduces identical bytes.

Worked examples (92-user test audience):

| Transition | Affected |
|---|---|
| disabled 0% → enabled 100% | 92 |
| enabled 100% → disabled 0% | 92 |
| enabled 100% → enabled 50% | 46 |
| enabled 50% → enabled 100% | 46 |
| disabled 0% → enabled 33% | 30 |
| enabled 33% → disabled 0% | 30 |
| disabled @80% → disabled @0% (config changes, exposure doesn't) | 0 |
| enabled 100% → enabled 100% (exact no-op) | 0 |

### Selector hashing

`set_feature_flag` reuses the exact same `proofgate.selector.
compute_selector_hash` function and canonicalization as the two
users-table tools -- no second hash implementation, no feature-flag
special case in the hash logic itself. Making this work required widening
`SELECTOR_FIELDS` (a plain data allowlist of every registered tool's own
selector-argument names) to also include `flag_name`, `enabled`, and
`rollout_percentage` alongside the existing `inactive_days`/`environment`
-- the canonicalization algorithm, key ordering, and serialization are
byte-for-byte unchanged, and `delete_users`/`deactivate_users` selector
hashes are unaffected (their arguments never contain the new keys).

### Broad request and exact outcome

```text
Instruction: "Enable the new checkout flow for test users."
Arguments: flag_name=new_checkout, enabled=true, environment=null, rollout_percentage=100

Verdict: BLOCK
Impact: 10,073 total (9,981 production, 92 test)
Triggered rules: RULE_INTENT_BOUNDARY, RULE_WORKFLOW_BUDGET
(RULE_RECOVERY_PROOF never fires -- see below)
Executed: false
Working feature-flag state: unchanged
```

### Corrected request and exact outcome

```text
Instruction: "Enable the new checkout flow for the test environment."
Arguments: flag_name=new_checkout, enabled=true, environment=test, rollout_percentage=100

Verdict: ALLOW
Mutation: affected=92, production_affected=0, test_affected=92
Postcondition: VERIFIED
Workflow budget: 92/100
new_checkout.test: {enabled: true, rollout_percentage: 100, version: 2}
new_checkout.production: unchanged (enabled: false, rollout_percentage: 0, version: 1)
Pristine feature-flag state: unchanged
```

An explicit, correctly-scoped production request (`environment=production`,
`rollout_percentage=100`) still triggers `RULE_WORKFLOW_BUDGET` alone
(9,981 exceeds the 100-row budget) -- explicit, correctly-represented
intent never overrides workflow-budget protection.

### Corrected disable, from enabled 100% (Slice 25.1)

```text
Setup: new_checkout.test = {enabled: true, rollout_percentage: 100}
Instruction: "Disable the new checkout flow for the test environment."
Arguments: flag_name=new_checkout, enabled=false, environment=test, rollout_percentage=0

Verdict: ALLOW
Impact: 92 (production=0, test=92)
Mutation: affected=92, production_affected=0, test_affected=92
Postcondition: VERIFIED
Workflow budget: 92/100
new_checkout.test: {enabled: false, rollout_percentage: 0, version: incremented}
new_checkout.production: unchanged
```

This is the scenario Slice 25's original formula got wrong (it would
have reported `0` affected users for this exact disable). Reducing an
existing 100% test rollout to 50% correctly reports `46` affected users;
increasing 50% back to 100% also correctly reports `46`.

### Recovery-proof reasoning

> Recovery proof is not required because this sandbox models the
> configuration update as reversible. Real provider propagation and
> downstream user effects may have different recovery characteristics and
> remain outside this local demonstration.

`set_feature_flag` is registered with `hard_delete=False`,
`reversibility="reversible"` -- the exact same registry metadata shape
`deactivate_users` already uses. `RULE_RECOVERY_PROOF` reads only
`ImpactEnvelope.hard_delete` (never a tool-name branch), so it never fires
for this tool; no snapshot is ever created or validated for it, and it
never enters Slice 23's automatic rollback (rollback's own eligibility
check requires `hard_delete=True`). Raw `proof_status` follows the
existing reversible-action contract (`"MISSING"`, meaning "none supplied,
none required" -- the same honest wire-format value `deactivate_users`
already produces, disambiguated the same way in any human-facing
presentation layer, per Slice 24).

### Workflow budget and postcondition

Unchanged, generic machinery: affected users count toward the same
workflow budget as any other tool (`92/100` for the corrected request; a
genuine no-op consumes `0`). Postcondition verification is the same
unmodified `verify_postcondition` comparing the deterministic preview's
predicted count against the mutation's own honestly-reported actual
count -- no feature-flag-specific verification logic exists.

### Repair guidance

A blocked broad rollout's suggested repair now names `set_feature_flag`
(never `delete_users`/`deactivate_users`), preserves `flag_name`,
`enabled`, and `rollout_percentage` unchanged, and corrects only
`environment` to `"test"`. Its `next_step` reads
`"retry_with_corrected_environment"`, never `"create_snapshot"` --
suggesting a snapshot for a reversible action would be actively
misleading. This generalization (Slice 25) also corrected
`deactivate_users`' own repair suggestion the same way, for the same
reason.

### MCP discovery and schemas

Tool discovery now returns exactly three tools: `deactivate_users`,
`delete_users`, `set_feature_flag`. `delete_users`/`deactivate_users`
still share the exact same `GuardedToolRequest` schema, byte-for-byte
unchanged. `set_feature_flag` has its own new `FeatureFlagToolRequest`
schema (`flag_name`, `enabled`, `environment`, `rollout_percentage`, plus
the same governance fields), selected via a registry-dispatch lookup
keyed by tool name -- never an `if tool_name == "set_feature_flag":`
branch anywhere in the guarded pipeline itself. The MCP handler never
imports `operations.feature_flags` directly; every governed call still
routes through the one shared `guarded_execute(...)` boundary.

### Reset

```python
from operations.feature_flags import reset_feature_flags_working
reset_feature_flags_working()
```

Restores `operations/feature_flags_working.json` to the exact pristine
seed. Affects only feature-flag state -- never the users database, never
the pristine file itself. Safe to call repeatedly.

### No Streamlit exposure, no real provider

This slice is a backend and MCP genericity proof only: `app.py` and
`app_logic.py` are completely unchanged, and no feature-flag selector,
button, or page was added anywhere in Streamlit. No real feature-flag
provider (LaunchDarkly, Unleash, ConfigCat, AWS AppConfig, or otherwise)
is contacted, and no network call is made anywhere in this tool's path.
Existing `delete_users`/`deactivate_users`/rollback behavior is completely
unaffected.

### Demonstration

```bash
source .venv/bin/activate
python scripts/feature_flag_demo.py
```

Local and deterministic: forces the MCP subprocess's own runtime mode to
`fallback` so no live Nebius call is attempted. Submits, in order, through
the real MCP stdio gateway: the broad enable, the corrected enable, the
corrected disable (from enabled 100%), a rollout reduction (100% → 50%),
and an exact no-op (independently verified to perform zero file writes).
Independently verifies the resulting working-state file after each
allowed action, confirms production and pristine state invariants,
writes a sanitized transcript to
`artifacts/feature_flag_demo_<timestamp>.json` (gitignored), and resets
feature-flag state, the users database, and the audit log afterward.
