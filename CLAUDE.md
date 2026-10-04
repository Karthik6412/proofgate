# Claude Code Instructions

@AGENTS.md

Read `ProofGate_PRD_FINAL_v4.md` before making architectural changes to the existing Operations domain.

Use the repo skills when their descriptions match the task:

- `proofgate-build-slice`
- `proofgate-demo-check`

## Current project: ProofGate Action Boundary

Source of truth: `docs/action-boundary-final-build-spec.md`. Read it before any Action Boundary slice.

Precedence for Action Boundary work:

- The Action Boundary spec wins on any conflict.
- `ProofGate_PRD_FINAL_v4.md` defines existing Operations behavior that must not regress.
- `AGENTS.md` invariants, safety rules and test discipline still apply. Its MVP Build Order, "do not broaden beyond the PRD", and Time Discipline exclusions are historical constraints on the original MVP and do not limit this project.
- The spec is explicit approval for exactly two frozen-architecture changes: inverting registry ownership and parameterizing policy by ruleset. Ask before any other architectural change.

Target architecture invariant (Slice 1 establishes it):

- Generalized core abstractions contain no legal-specific branches and no `if domain == ...` branching.
- `proofgate/registry.py` does not own or import concrete domain implementations; the Operations binding (`proofgate/bindings/operations.py`) registers into it. Registry and generic policy never import a binding.
- `proofgate/policy.py` is a generic Rule/RuleSet engine that never knows a concrete context; domains supply their context type and ruleset.
- `guarded_execute` remains the Operations execution pipeline. Do not generalize it. Legal gets its own resolve → authorize → approve (when required) → execute → verify → audit pipeline that reuses the policy engine, canonical hashing and audit persistence (decision, 2026-10-04).
- Existing Operations imports in `proofgate/rollback.py` and `proofgate/proofs.py`, and the `agent.nebius_client` import in `proofgate/core.py`, are out of scope for v1 unless Slice 1 proves they block the new architecture.

Workflow budget: `RULE_WORKFLOW_BUDGET` stays one generic rule; each domain defines its unit (Operations: affected rows; Legal: unique documents subject to a consequential action), and audit must name the unit. Slice 1 leaves `WorkflowBudget` unchanged; Legal accounting lives in the Legal pipeline.

Milestone: Friday 2026-10-09 is Action Boundary v1 (see the spec's "Ships Friday" scope). Items the spec marks post-Friday continuation scope are not to be implemented, shown or claimed as done before then unless explicitly requested.

Models may propose actions; they never issue authoritative ALLOW/BLOCK. Unknown or unverifiable consequential impact fails closed.

## Working rules

- Implement one vertical slice at a time.
- Inspect and plan before editing.
- Do not implement future slices unless explicitly requested.
- Run relevant tests after every slice.
- Preserve existing ProofGate behavior and the full regression suite.
- Do not broaden scope.
- Do not add dependencies without explaining why.
- Do not alter the guarded ProofGate versus dumb Operations contract.
- Never claim completion without listing exact commands and results.
- Surface failures honestly.
- Prefer a working terminal flow over unfinished UI polish.

## Slice report

At the start of every implementation slice or phase, before investigating or editing, run `date "+%Y-%m-%d %H:%M:%S %z"`; its exact output is the start timestamp. At the end, run the same command again and compute elapsed wall-clock time from the two observed timestamps.

Before the final summary, output the following lines exactly, not wrapped in a Markdown code fence:

SLICE REPORT
date:      <observed end timestamp>
slice:     <slice/phase id and one-line goal>
started:   <observed start timestamp, else "unknown">
actual:    <elapsed wall-clock calculated from observed start/end timestamps, else "unknown">
files:     <count derived from git diff/status for this slice or phase>
tests:     <actual pass/fail counts from test command output before and after>
reruns:    <failed or reworked attempts directly observable in this session, else "unknown">
friction:  [model self-report] <specific thing you got wrong or were corrected on; "none" if nothing>
missing:   [model self-report] <anything in CLAUDE.md, AGENTS.md or the spec that was absent, ambiguous or contradictory and cost time; "none" if nothing>
evidence:  <briefly name the command/output sources used for the measured fields>
END REPORT

- Measured fields come only from observable command output or visible session history. Never reconstruct or guess a timestamp, elapsed time, test count, file count or rerun count from memory; write "unknown" when the evidence is unavailable.
- `friction` and `missing` are retrospective model self-reports, not telemetry and not a trace of model inference. Do not claim the report captures internal reasoning or inference steps.
- Do not estimate how long the work should have taken. The human records the estimate before the slice starts.
- Do not comment on whether the slice went well.
- Do not create, write to or modify `notes/log.md` or any other log file. The human pastes the report manually.
