---
name: proofgate-demo-check
description: Use this skill when asked to validate that the ProofGate demo works end-to-end, before declaring the core demo complete, before a rehearsal, or before submission. Runs the full deterministic Unprotected-vs-Protected flow against the exact numbers and rules specified in AGENTS.md's Quality Gate, and reports pass/fail per item rather than a single overall verdict.
---

# ProofGate Demo Check

## When to use this

- Before declaring any milestone "demo-complete."
- Before every rehearsal run.
- Before submission.
- Any time the user asks "does the full demo work" or similar.

Do not use this for single-slice unit testing — that belongs to
`proofgate-build-slice`. This skill is for full end-to-end validation only, and
assumes all relevant slices already claim to be individually working.

## Procedure

Run these in order. Do not skip ahead if an earlier step fails — report the failure
and stop, since later steps depend on earlier state.

### 1. Reset

- Run the reset command (pristine → working).
- Verify `operations/working.db` matches seed exactly.

### 2. Seed verification

- Confirm exactly:
  - 9,981 inactive production users
  - 92 inactive test users
  - 10,073 total inactive users

### 3. Unprotected flow

- Run the broad call directly through `operations.delete_users` (bypassing
  ProofGate).
- Confirm exactly 10,073 rows deleted.
- Reset immediately after.

### 4. Protected flow — broad bad call

- Run the same instruction through the guarded path.
- Confirm impact preview reports 10,073 (9,981 production / 92 test).
- Confirm verdict is `BLOCK`.
- Confirm **zero mutation occurred** — query the working DB directly to verify row
  count is unchanged, don't just trust the returned verdict.
- Confirm triggered rules are exactly:
  - `RULE_INTENT_BOUNDARY`
  - `RULE_RECOVERY_PROOF`
  - `RULE_WORKFLOW_BUDGET`
- Confirm `RULE_UNKNOWN_IMPACT` did **not** fire (preview succeeded, so it shouldn't).

### 5. Repair and proof

- Confirm the structured repair suggests `environment="test"`.
- Confirm corrected preview reports 92 test / 0 production.
- Confirm a snapshot is created for the corrected selector.
- Confirm the snapshot resource is `users`.
- Confirm the selector hash on the proof matches the corrected arguments
  (`{"environment": "test", "inactive_days": 90}` canonicalized) — not just that a
  hash exists, but that it's the *correct* one.

### 6. Protected flow — corrected call

- Retry the same public `delete_users` tool with corrected arguments and valid proof.
- Confirm verdict is `ALLOW`.
- Confirm exactly 92 rows mutated, 0 of them production.

### 7. Postcondition

- Confirm predicted count (92) equals actual count (92).
- Confirm production affected = 0.
- Confirm status is `VERIFIED`.

### 8. Workflow budget

- Confirm budget now reads 92/100.
- Optionally: attempt one more action that would push cumulative mutation past 100,
  confirm it is rejected by `RULE_WORKFLOW_BUDGET`.

### 9. Audit trail

- Confirm the JSONL audit log contains both a `BLOCK` event (broad call) and an
  `ALLOW` event (corrected call), each with the fields specified in the PRD's audit
  schema.

### 10. UI correctness (if UI slice is built)

- Confirm the UI shows risk factors and triggered rules as visually separate
  concepts.
- Confirm the UI renders only rules that actually fired — no canned/static reason
  list.
- Confirm CRAFT evidence and Operations impact are labeled and displayed separately.

## Reporting format

Report every checklist item individually as PASS / FAIL / NOT YET BUILT — never
collapse this into a single "demo works" statement. For any FAIL, include the exact
command run and the exact output received, not a description of what you expected.

End with:

- Overall readiness for rehearsal (yes/no).
- If no: the single highest-priority fix needed first.
- Confirmation this satisfies (or does not yet satisfy) AGENTS.md's Quality Gate in
  full.
