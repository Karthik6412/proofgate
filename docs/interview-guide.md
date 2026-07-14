# Interview and Ownership Guide

This document helps the repository owner explain the work accurately --
it is a study aid, not a claim that reading it substitutes for actually
understanding the system. See the ownership checklist at the end before
presenting this project as something you can defend live.

## 90-second explanation

ProofGate sits between an AI agent and a consequential tool call. When an
agent proposes something like `delete_users(inactive_days=90)`, being
*allowed* to call that tool isn't the same as this *specific invocation*
being safe -- a missing `environment="test"` filter is the difference
between deleting 92 test accounts and deleting 10,073 records, 9,981 of
them production. ProofGate measures the real blast radius before
execution, runs it through a deterministic policy (never an LLM's
opinion) that checks intent boundaries, recovery proof, and a cumulative
workflow budget, and only then lets the real mutation happen. After
execution it verifies the outcome matched the prediction, and for the
one irreversible action it governs, it can automatically detect a
genuine mismatch, restore from a validated snapshot, and independently
verify the restoration before calling it done. A third tool on a
completely different resource (a feature-flag JSON file, not the SQL
database) proves the whole pipeline is generic, not hardcoded to one
table.

## Five-minute technical walkthrough

**Problem.** Tool-level authorization ("can this agent call
`delete_users`") says nothing about whether *this* call, with *these*
arguments, is safe. Agents can drop a filter, misinterpret scope, or be
manipulated into a broader call than intended.

**Architecture.** MCP gateway → registry lookup → semantic extraction
(Nebius, advisory only) → deterministic impact preview → deterministic
policy → ALLOW/BLOCK → Operations execution (only on ALLOW) →
postcondition verification → rollback resolution when eligible → one
audit event. See `docs/architecture.md` for the diagrams.

**One BLOCK.** `delete_users(inactive_days=90, environment=None)` against
the deterministic seed previews at 10,073 rows, 9,981 production. Policy
triggers `RULE_INTENT_BOUNDARY` (the instruction implied test scope, the
call didn't), `RULE_RECOVERY_PROOF` (irreversible, no proof), and
`RULE_WORKFLOW_BUDGET` (10,073 exceeds the 100-row budget). Zero rows
mutated.

**One ALLOW.** The corrected call, `environment="test"`, with a valid
server-validated snapshot proof, previews at 92, executes, and
postcondition verification reports `VERIFIED` (92 predicted, 92 actual).

**Recovery proof.** Not a caller-supplied claim -- the server looks up
the snapshot by ID and cross-checks every field of the caller's proof
against its own stored metadata. A technically valid proof still never
overrides an intent-boundary or budget violation.

**Repair.** After a BLOCK, one bounded, non-authoritative repair attempt
is possible: a model gets the original proposal, the real verdict, real
triggered rules, and the real (unmodified) suggested repairs, and
proposes exactly one revision. The revision is re-validated and
re-evaluated through the same real policy path -- the repair model never
authorizes anything itself.

**Rollback.** For the one irreversible tool, if postcondition
verification detects a genuine mismatch and a valid recovery proof
exists, the system restores from the snapshot and independently
re-verifies the restoration (a separate digest/count check, not the same
mechanism as normal postcondition verification) before reporting
`ROLLED_BACK`. Budget still shows the real total mutation (`93/100` in
the demo, since the mismatch demo itself performs one real extra
deletion) -- rollback never refunds it.

**Third-tool genericity.** `set_feature_flag` operates on a filesystem
JSON resource with a completely different selector shape and mutation
mechanism, added with exactly one new registry entry and zero new
branches in policy, core, budget, proof, postcondition, or audit code --
verified by AST-based tests that assert no tool-identity comparison
exists in any of those files.

**Limitations.** Single-process only; no concurrency control; no
authentication; local, non-tamper-evident audit; rollback safety depends
on an isolated single-process assumption; feature-flag impact is a local
deterministic stand-in, not a real provider integration. See
`THREAT_MODEL.md`.

## Deep-dive questions and accurate answers

**Why is RBAC insufficient?**
RBAC answers "is this agent allowed to call this tool at all." It says
nothing about whether *this specific invocation*, with *these specific
arguments*, is safe. An agent can be authorized to call `delete_users`
and still submit a call that deletes 10,073 rows instead of 92 -- RBAC
has no concept of measuring that difference before it happens.

**Why deterministic rules instead of one LLM judging another?**
Determinism, reproducibility, and resistance to being talked out of a
verdict. The project specifically exercised a scenario where the live
model's semantic extraction was wrong or adversarial, and the verdict
was still correct, because the policy engine never reads the model's
opinion -- only structured signals plus the real, independently computed
impact count. An LLM judging another LLM's proposal has no such
guarantee; both are subject to the same class of failure.

**Why MCP?**
It's a real, adopted protocol for how agents actually discover and call
tools, not a bespoke API built to look good in a demo. Guarding a real
MCP stdio gateway proves the enforcement boundary works against the same
interface a real agent integration would use.

**What is blast radius?**
The actual, measured impact of a consequential action -- the real number
of records (or, for feature flags, the real number of users whose
effective exposure changes) an action would affect, computed
deterministically before execution.

**What makes recovery proof tamper-resistant?**
Every field of a caller-supplied proof is cross-checked against
server-side metadata looked up by `snapshot_id` -- the caller cannot
choose the ID, and cannot inflate `max_affected_rows` or forge a
`selector_hash` and have it accepted, because validation only trusts the
stored record, never the caller's copy.

**What does recovery proof not guarantee?**
It does not guarantee authorization -- a valid proof never overrides
`RULE_INTENT_BOUNDARY` or `RULE_WORKFLOW_BUDGET`. It is not a
single-use or revocable token: the same proof remains valid for repeated
use against the same selector for as long as its snapshot file exists.
It says nothing about real-world (production-system) reversibility --
only that a local snapshot exists.

**Why does rollback not refund budget?**
Because the original consequential execution genuinely happened and
consumed real operational risk at the time it ran, independent of
whether its database effects were later reversed. Budget measures that
execution occurred, not the data's current state.

**Why does the rollback demonstration consume 93 rather than 92?**
The demonstration's mismatch-injection mechanism performs one additional,
real deletion (of a test row outside the intended selector) so that the
unmodified postcondition verifier can detect a genuine mismatch rather
than a fabricated one. The real total mutation was honestly 93 rows, so
budget honestly reflects 93, not the originally-intended 92.

**How is `ROLLED_BACK` produced?**
After an ALLOWed, executed, rollback-eligible action (hard-delete tool,
valid proof) reports a genuine `MISMATCH` from the unmodified
`verify_postcondition`, `proofgate/rollback.py` checks eligibility,
invokes the trusted `restore_snapshot` operation, and then independently
recomputes a digest/row-count comparison against the snapshot -- only if
that independent check agrees does the final status become
`ROLLED_BACK`. All of this happens inside one synchronous
`guarded_execute` call, before the single audit event is written.

**What happens when restoration fails?**
Any failure -- missing or corrupted snapshot, selector mismatch,
conflicting row, transaction rollback, or a disagreement in the
independent verification step -- produces `MANUAL_REVIEW_REQUIRED`
instead. The system never reports `ROLLED_BACK` on a restore function's
self-report alone.

**Why does feature-flag disable affect 92 users?**
Because blast radius for feature flags is the change in *effective
exposure*, not the requested rollout percentage in isolation. Disabling
a flag previously at 100% rollout for the 92-user test audience means
all 92 of them lose access -- that is the honest, real number of users
affected.

**Why is feature-flag impact based on exposure delta?**
The original formula (`audience * requested_rollout_percentage`) silently
reported 0 impact for a full disable, which is wrong -- disabling
clearly changes what real users experience. Comparing current vs.
requested effective exposure and taking the absolute delta makes
enabling, disabling, increasing, and decreasing rollout all honestly
accounted for.

**How does the registry prove genericity?**
`set_feature_flag` -- a different resource (filesystem JSON, not SQLite),
a different selector shape, and a different mutation mechanism -- was
added as exactly one new `GuardedToolSpec` entry, with zero new branches
in policy, core, budget, proof, postcondition, or audit code. AST-based
tests assert no tool-identity string comparison exists anywhere in those
files.

**How is direct MCP-to-Operations bypass prevented?**
The MCP handler only ever calls `guarded_execute(...)`; it never imports
`operations.actions` or `operations.feature_flags` directly (checked by
an AST-based test). Operations functions themselves accept no
`ActionContext`, proof, or policy metadata, so there's nothing for a
bypassing caller to supply that would make a direct call look governed.

**What security boundary is only conventional rather than cryptographic?**
The Operations/guarded-execute module boundary. Nothing prevents
same-process Python code with import access from calling
`operations.actions.delete_users` directly -- it's enforced by code
structure, review, and tests, not by process isolation or cryptographic
access control.

**What are the largest production gaps?**
No concurrency control or durable cross-process state; no crash recovery
after abrupt termination; no authentication, authorization, or tenant
isolation; a local, non-tamper-evident audit log; a rollback mechanism
whose safety depends on a single-process assumption; and no real
external provider integration for feature flags.

**How would concurrent recovery work?**
It doesn't today -- rollback safety currently depends on an isolated
single process with no concurrent writers. A production version would
need idempotency keys, optimistic concurrency/version checks, durable
workflow state across restarts, coordination locks around restoration,
and reconciliation after the fact.

**Why was outbound notification not selected as the third tool?**
Notification delivery is inherently irreversible once sent. Supporting
it honestly would have required redefining recovery proof as "audience
approval" rather than "recoverability," inventing a new proof type,
adding human approval, or permanently blocking the corrected case -- any
of which turns a genericity proof into a proof-model redesign. Feature
flags stay reversible under the *existing* proof semantics, isolating
the genericity question from the proof-model question.

**What would change for a real feature-flag provider?**
Impact could no longer come from a local static audience count -- it
would need the provider's own preview/dry-run API if one exists, handle
propagation delay and eventual consistency, support remote idempotency
for retried calls, and reconcile/monitor to confirm the change actually
took effect downstream.

## Ownership checklist

Before presenting this project, confirm you can, unaided:

- [ ] trace one BLOCK end to end (client call → preview → policy → audit)
- [ ] trace one ALLOW end to end, including postcondition verification
- [ ] explain all four policy rules and when each fires
- [ ] explain selector hashing and why it binds a proof to an exact selector
- [ ] explain proof validation and why the caller's copy is never trusted
- [ ] explain workflow budgets and why rollback doesn't refund them
- [ ] explain postcondition verification (count-based, not digest-based)
- [ ] explain rollback eligibility (hard-delete, valid proof, genuine mismatch)
- [ ] explain independent restoration verification and how it differs
      from normal postcondition verification
- [ ] explain why the rollback demo shows 93/100, not 92/100
- [ ] explain feature-flag exposure accounting (why disable affects 92)
- [ ] explain the single-process concurrency limitation honestly
- [ ] run `scripts/run_portfolio_demo.py` unaided and narrate it live
- [ ] identify which parts of this repository were model-assisted, and
      explain specifically how you reviewed and verified that work
      (tests you ran, numbers you checked, code you read line by line)
      rather than simply asserting it is correct

Do not invent résumé metrics (users served, dollars saved, latency
numbers) that this local reference implementation cannot actually
support.
