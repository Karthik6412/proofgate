# ProofGate Starter Pack

Copy the contents of this folder into the root of the ProofGate repository.

Included:

- `ProofGate_PRD_FINAL_v4.md` — final build specification
- `AGENTS.md` — shared repository instructions
- `CLAUDE.md` — Claude Code entry instructions
- `.claude/skills/proofgate-build-slice/SKILL.md` — one-slice-at-a-time implementation workflow
- `.claude/skills/proofgate-demo-check/SKILL.md` — full deterministic demo validation workflow

## Start prompt for Claude Code

```text
Read CLAUDE.md, AGENTS.md, and ProofGate_PRD_FINAL_v4.md.

Use the proofgate-build-slice skill.

Implement only Slice 1:
- deterministic SQLite schema and seed
- pristine-to-working reset
- preview_delete_users
- exact tests for 9,981 production, 92 test, and 10,073 broad impact

Do not start Nebius, CRAFT, Streamlit, snapshots, policy, or MCP yet.

Run the tests and stop with:
- files changed
- commands run
- exact test results
- next recommended slice
```

## First architecture agreement with your teammate

Freeze this sentence before parallel work:

> `rollback_proof` is accepted and validated by the public ProofGate action boundary. The internal Operations mutation remains dumb and never accepts proof or policy context.
