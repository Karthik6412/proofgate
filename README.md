# ProofGate

### Blast-radius enforcement for AI agents

**Dangerous actions need proof, not promises.**

---

Enterprise agents are moving from *reading* data to *changing* real systems. Access control can tell an agent whether it is allowed to call `delete_users`. It cannot tell you whether this exact invocation, with these exact arguments, deletes 2 test accounts or 10,073 real customers.

ProofGate is the layer that answers that question, at runtime, before the action executes.

## The failure this exists to stop

```
User:   "Clean up inactive test accounts that haven't logged in for 90 days."
Agent:  delete_users(inactive_days=90)
```

One missing filter. `environment="test"` never made it into the call.

```
Without ProofGate:  10,073 users deleted. 9,981 of them production.
With ProofGate:     BLOCKED. Risk 9.5/10. Zero rows touched.
```

The tool call was authorized. The arguments were catastrophic. That gap is the whole product.

## How it works

```
Agent proposes an action
        │
        ▼
Impact preflight ─── measures exact blast radius against the real system
        │
        ▼
Nebius Token Factory ─── extracts structured risk signals (never a verdict)
        │
        ▼
Deterministic Python policy ─── the only thing that decides ALLOW or BLOCK
        │
        ▼
BLOCK ──► structured repair guidance, zero mutation
ALLOW ──► requires a server-validated snapshot proof, then executes
        │
        ▼
Postcondition verification ─── checks the real outcome matches the prediction
        │
        ▼
Append-only audit trail
```

**The model never makes the security decision.** Nebius reads intent and proposed arguments and extracts semantic features like *intent mismatch* or *missing constraints*. Deterministic Python rules, working only off authoritative counts from the database that's actually being mutated, decide the verdict. We proved this holds even when Nebius is fed a response that lies about the impact being safe — the broad action still gets blocked, because the policy engine never trusts the model's opinion.

## What makes a proof real

A rollback proof isn't a caller-supplied claim. When an agent requests a snapshot, ProofGate stores the authoritative metadata server-side and validates every future check against that record, not against whatever the agent hands back. An agent cannot inflate `max_affected_rows` or forge a `selector_hash` and have it accepted, because validation never trusts the caller's copy of the truth.

And proof is never authorization. A technically valid snapshot for a production-impacting, intent-mismatched action still gets **BLOCKED**. Recoverability and permission are two different questions, and ProofGate never conflates them.

## Sponsor integration

| | Role |
|---|---|
| **Nebius Token Factory** | Live intent extraction and structured risk-feature extraction, `nvidia/nemotron-3-super-120b-a12b`, strict JSON, deterministic fallback on any failure |
| **Emergence CRAFT** | Real, live enterprise schema discovery and natural-language-to-SQL cohort evidence, clearly separated from the authoritative mutation impact |

Both are wired for real, load-bearing use, not a checkbox integration. Neither can override the deterministic verdict.

## Stack

Python · SQLite · Pydantic · Streamlit · the official `mcp` SDK (OAuth 2.1 + PKCE) · pytest

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
pytest tests/ -q
streamlit run app.py
```

See `DEMO.md` for the exact click path and expected numbers.

## What's tested

Nearly 300 tests, covering:

- All four deterministic hard-block rules, individually and in combination
- Proof validation against tampered/forged caller-supplied fields
- The proof-does-not-override-intent guarantee
- Cumulative workflow budget enforcement across multiple calls
- Postcondition verification against real mutation results
- A full BLOCK → repair → ALLOW → audit loop, end to end
- Adversarial Nebius responses that lie about risk

## Roadmap

- Snapshot restoration and rollback execution
- Human-in-the-loop approval tier
- Transparent MCP proxy for arbitrary downstream action servers
- Additional action types beyond `delete_users`

## Built by

Karthik Nair & Disha, at the Enterprise Agents Hackathon by Emergence AI, in partnership with Nebius.
