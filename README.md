# ProofGate

> **Dangerous actions need proof, not promises.**

ProofGate is a runtime safety layer for AI agents with access to real enterprise systems.

It intercepts consequential tool calls, calculates their exact blast radius before execution, and uses deterministic policy to block dangerously broad actions until the agent provides machine-verifiable evidence that the operation is properly scoped and recoverable.

---

## The problem

AI agents are increasingly gaining write access to:

- production databases
- CRMs
- customer communication tools
- cloud infrastructure
- internal enterprise systems

Traditional access control can answer:

> “Is this agent allowed to call this tool?”

But it cannot answer:

> “Is this specific tool call dangerously broad?”

An agent may be authorized to delete inactive test users, but accidentally issue:

```python
delete_users(inactive_days=90)
