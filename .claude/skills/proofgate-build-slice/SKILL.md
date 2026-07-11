---
name: proofgate-build-slice
description: Use this skill whenever implementing a single vertical slice of ProofGate (any item from the Build Order in AGENTS.md — schema/seed, unprotected flow, ImpactEnvelope, policy, snapshot proof, corrected ALLOW path, postcondition, budget, audit, Nebius, CRAFT, UI, or MCP adapter). Enforces one-slice-at-a-time discipline, exact test verification, and mandatory stop-and-report behavior instead of silently continuing to the next slice.
---

# ProofGate Build Slice

## When to use this

Any time you are about to write or modify code for ProofGate. This applies to every
item in AGENTS.md's Build Order — there is no such thing as a ProofGate code change
that falls outside this workflow.

## Before starting a slice

1. Confirm which single slice you are implementing. State it explicitly in one line
   (e.g. "Implementing Slice 3: ImpactEnvelope and preview").
2. Re-read the relevant sections of `ProofGate_PRD_FINAL_v4.md` and `AGENTS.md` for
   that slice only — Architectural Invariants, Frozen Architectural Boundary,
   Selector Hash Contract, Operations Boundary Correctness, and Policy Rules as
   applicable.
3. State any assumptions you're about to make before writing code, not after.
4. Do not start a second slice while a prior slice is incomplete or unverified.

## While implementing

- Touch only the files required for this slice. Do not opportunistically refactor
  unrelated code.
- Do not introduce dependencies outside the Approved Dependencies list in AGENTS.md
  without asking first.
- Do not blur the Frozen Architectural Boundary: `guarded_delete_users` (ProofGate,
  policy-aware) vs `operations.delete_users` (dumb, no policy/proof awareness) must
  stay separated exactly as specified.
- If this slice touches selector hashing, snapshot proof, or impact preview, use the
  single shared canonicalization function — never a second implementation.
- Follow the 35–40 minute time-box in AGENTS.md's Time Discipline section. If you hit
  it without passing tests, stop and report rather than continuing to iterate.

## After implementing — mandatory report

Every slice ends with a report containing exactly these sections, even if the slice
did not fully succeed:

1. **Files changed** — exact paths.
2. **Commands run** — exact commands, verbatim.
3. **Exact results** — real test/command output, not a paraphrase or summary claim.
4. **Unverified assumptions** — anything you assumed but did not confirm.
5. **Next recommended slice** — per the Build Order in AGENTS.md.

Do not say "this works" or "this is complete" without the exact command output to
back it up (see AGENTS.md's Completion Standard).

## Hard stop conditions

Stop and report immediately, without proceeding to the next slice, if any of the
following occur:

- Tests for this slice fail after the time-box.
- You find yourself about to modify the Frozen Architectural Boundary.
- You find yourself about to duplicate selector-hash logic.
- You find yourself about to let Operations evaluate intent, risk, proof, or budget.
- You are unsure whether a change broadens scope beyond the PRD.

In every hard-stop case: report what currently works, what doesn't, and the smallest
viable fallback — then wait for explicit direction before continuing.
