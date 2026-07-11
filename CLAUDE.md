# Claude Code Instructions

@AGENTS.md

Read `ProofGate_PRD_FINAL_v4.md` before making architectural changes.

Use the repo skills when their descriptions match the task:

- `proofgate-build-slice`
- `proofgate-demo-check`

## Working rules

- Implement one vertical slice at a time.
- Run relevant tests after every slice.
- Do not broaden scope.
- Do not add dependencies without explaining why.
- Do not alter the guarded ProofGate versus dumb Operations contract.
- Never claim completion without listing exact commands and results.
- Surface failures honestly.
- Prefer a working terminal flow over unfinished UI polish.
