# ProofGate Action Boundary — Final Build Spec

Oct 4, 2026 · @Karthik Nair

The Action Boundary v1 milestone ships Friday October 9, 2026 (scope under "Thesis and what ships"); everything else in this spec is the full-scope destination, marked as post-Friday continuation scope where it appears. Supersedes the v1 spec and the Action Boundary v2 draft.

## Thesis and what ships

**Permission is not authorization.** A user or agent may be permitted to access a resource or call a tool, while a specific invocation of that tool still exceeds what the human actually authorized.

Access control answers: *can this user read this document, can this agent call this tool, can this recipient receive documents?* Existing access-control systems address those questions and remain authoritative; integrating and enforcing their decisions correctly is still required. This layer answers a different one:

> Does this exact multi-step action, with these arguments, against the current system state, stay inside the authority the human granted?

Legal is the flagship domain because it makes the gap unmissable: the coarse tool and matter permission checks pass, the lawyer legitimately has access to several matters, the agent is legitimately allowed to search and compile and share — and the resolved workflow still touches the wrong matter, too many documents, privileged content, or an unapproved recipient.

**The engineering claim is not that legal AI needs a safety wrapper.** It is that ProofGate's deterministic policy engine generalizes across unrelated domains once its hidden domain couplings are removed, while each domain keeps the execution lifecycle its semantics require. The legal demo is the evidence; the refactor is the result.

**Ships Friday October 9 (Action Boundary v1):** the generalization refactor (registry inversion and a generic policy engine), with the full existing Operations regression suite still passing; an `AuthorityGrant`; a seeded legal corpus and permission snapshot; a multi-step read-only resolver producing a `ResolvedEffect`; the four deterministic legal rules, run by the generic engine without legal-specific branches; an end-to-end authorized-vs-resolved BLOCK; an authorization/action digest, and a tamper attempt that fails because the digest no longer matches; a decision/audit record showing authorized vs resolved values; and a recorded demo of exactly that.

**Post-Friday continuation scope** (designed in this spec, not shipped Friday, and never shown or claimed in the Friday README or video): full permission re-fetch and staleness handling, persistent cross-workflow unique-ID budget semantics, the REQUIRE\_APPROVAL layer, simulated execution/outbox, identity-level postcondition verification, the canary fault-injection scenario, and the v2 design document.

## The canonical failure

One scenario, used in the demo, the README, the audit sample and the post. The earlier drafts carried two competing figure sets; this is the only one.

**Human instruction**

```
"Prepare the diligence package for Matter M-102 and share the
non-privileged documents with outside counsel."
```

**Authority grant:** Matter M-102, max 100 documents, privilege excluded, recipient `counsel@example.com`.

**Agent plan**

```
search_documents(matter="M-102", query="diligence")
compile_package(documents=$search_documents.result, include_related=True)
share_package(recipient="counsel@example.com")
```

**Composition/expansion:** `search_documents` correctly returns only M-102 documents. `compile_package(..., include_related=True)` then follows seeded related-document links and expands package membership to include M-118 documents. The user legitimately has access to both matters, but granted this workflow authority only for M-102. Cross-matter scope arises from this second step, never from a broken search filter.

**Permission checks:** user may access M-102 and M-118 — PASS. Agent may search — PASS. Agent may package — PASS. Recipient permitted — PASS. These coarse checks do not waive document-level restrictions; the trusted resolver may inspect restricted metadata, but normal flow excludes restricted text from model context and denies execution involving restricted documents.

**Resolved effect**

|  | Authorized | Resolved |
| --- | --- | --- |
| Matters | 1 (M-102) | 2 (M-102, M-118) |
| Documents | ≤ 100 | 143 |
| Privileged | 0 | 9 |
| Recipients | 1 | 1 |

**Verdict:** `matter_boundary` FAIL, `privilege_boundary` FAIL, `workflow_budget` FAIL, `recipient_boundary` PASS → **BLOCK**.

The system is useful precisely because the permission checks were not wrong.

> **These numbers are provisional.** They are replaced with real output from the seeded corpus on Day 2, before anything is written down anywhere public. No figure in the README, video or post is invented.

## Where this is built

Inside the existing ProofGate repository, as a second governed domain beside `operations/`. Not a separate project: the claim is that the policy engine generalizes, and that is only verifiable if the legal layer imports the same engine and primitives rather than copying them.

**Reuse what is genuinely generic:** the Rule/RuleSet policy engine in `policy.py` (after Slice 1), canonical serialization and hashing in `selector.py`, and JSONL persistence in `audit.py`. `models.py`, `postcondition.py` and `budgets.py` are Operations-shaped (`ImpactEnvelope`, count-based postconditions, row budgets); Legal defines its own types for those concerns rather than bending them, and Operations behavior is preserved unchanged. `RULE_WORKFLOW_BUDGET` remains the generic rule concept; each domain owns its budget unit. `registry.py`'s `GuardedToolSpec` (`preview_fn`/`mutation_fn` returning `ImpactEnvelope`/`MutationResult`) is the Operations execution seam; the Legal resolver does not plug into it.

**Two couplings to break (Slice 1).**

1. **Policy rules reach into domain fields.** `evaluate_policy` has a generic signature but its rules read `environment_counts["production"]`, `hard_delete` and `rows_mutated`. Parameterize it by ruleset. Core never branches on domain.

```python
def evaluate_policy(context: ContextT, ruleset: RuleSet[ContextT]) -> PolicyDecision: ...

class Rule(Protocol[ContextT_contra]):
    rule_id: str
    def evaluate(self, context: ContextT_contra, /) -> RuleOutcome: ...
```

The engine never knows a concrete context. Operations supplies `OperationsPolicyContext` and its rules from `proofgate/bindings/operations.py`; Legal will supply its own context (grant, resolved effect, workflow state) the same way.

2. **`registry.py` imports `operations` at module level.** Invert it — the Operations binding registers its tools into the registry, never the reverse.

```
before:  registry                       ->  operations
after:   proofgate/bindings/operations  ->  registry, policy, operations
         legal pipeline                 ->  policy (Rule/RuleSet), selector, audit primitives
```

**Do not** add legal fields to the core `ImpactEnvelope`. Legal concepts in core would quietly destroy the generalization claim. Keep the effect generic and let domains carry their own dimensions (see the resolver section).

`rollback.py` and `proofs.py` also import `operations`, but real rollback is out of v1 scope — leave them untouched and do not call them.

**Day 1 warning.** The existing tests patch `operations.actions` directly, which is why `registry.py` uses call-time module-attribute lookup. Those patch points survive inversion as long as the call-time wrappers move into the Operations binding unchanged; tests that assert "not called" pass trivially if they don't, so keep the positive-control tests.

The refactor is itself a deliverable, with a deliberately bounded claim: "adding a second domain required inverting the registry, parameterizing the policy engine by ruleset, and giving the new domain its own authorization-bound execution path that reuses that engine." The Operations `guarded_execute` pipeline stays Operations-specific by design: its preview → mutate → count-verify lifecycle is not the resolve → authorize → approve → execute → identity-verify lifecycle legal actions need, and forcing both through one pipeline would mean either loosening Operations' frozen contract or hiding legal semantics behind Operations types. The tradeoff is two execution paths to maintain, in exchange for one shared, domain-agnostic verdict authority that both must use. That is still a checkable claim about generality, and a more defensible one.

## Legal data model

**Document**

```python
Document(doc_id, matter_id, client_id, title, status, document_type,
         privileged: bool, restricted: bool, canary: str | None, text)
```

`matter_id` is the principal workflow boundary. `client_id` supports client-level examples. `privileged` is deterministic metadata. `restricted` represents an external wall result. `canary` exists only for the leakage demo.

**AuthorityGrant** — the central contract. What the human actually approved, never inferred after the fact from the agent's plan.

```python
AuthorityGrant(grant_id, workflow_id, matter_id, max_documents,
               privilege_policy: Literal["exclude", "include"],
               allowed_recipients: list[str],
               granted_by, granted_at, expires_at)
```

**PermissionSnapshot** — consumed, never owned. In production the firm's IAM or ethical-wall provider stays authoritative; this layer reads its state and does not replace it.

```python
PermissionSnapshot(user_id, permitted_matters: set[str],
                   restricted_document_ids: set[str],
                   permitted_recipients: set[str], source_version, captured_at, snapshot_digest)
```

The snapshot digest covers canonical permission state (user, permitted matters, restricted document IDs, permitted recipients, and source version), with sorted sets. `captured_at` records freshness but is excluded from the digest so an unchanged refresh does not invalidate approval merely because time passed. The seeded local permission provider is authoritative for this synthetic v1 demo.

**Corpus.** 100–200 documents, 3 matters, 2 clients, mixed statuses and privilege labels, some restricted, some canary-bearing. Seeded and deterministic: same seed → same corpus → same demo numbers. Determinism matters more than realism; synthetic text is fine.

## Resolver and trust boundary

The component carrying the thesis. If the week compresses, everything else goes first.

**Input** is the agent's plan as ordered tool calls with arguments and references between steps (`$search_documents.result`). **Output** is the resolved effect — generic, so the policy engine stays domain-free:

```python
ResolvedEffect(
    total_affected=143,
    resource_ids=[...],
    dimensions={
        "matters_touched": {"M-102": 126, "M-118": 17},
        "privileged_count": 9,
        "restricted_count": 4,
        "external_recipients": ["counsel@example.com"],
    },
)
```

A typed `LegalResolvedEffect` wrapper is fine; the generic policy engine never sees either — legal rules receive it through the Legal policy context.

**Resolution rules.** Read-only retrieval executes for real — it must, or the counts are guesses. Side-effecting steps resolve their targets and stop.

```
search_documents  -> execute, read-only
compile_package   -> resolve membership
share_package     -> resolve recipient, DO NOT SEND
```

The principle: **resolve the real effect, not the apparent plan text.** The search names one matter; downstream related-document expansion resolves to two. The expansion option is visible in the plan, but its concrete membership is known only after resolution.

**Trust boundary.** The trusted resolver inspects metadata the agent must never see — doc IDs, labels, matter IDs, privilege flags, counts — but restricted document *text* never crosses back to the agent. Impact measurement must not itself become the leak.

**Known limitation, stated in the README.** Each tool needs a resolver implementation, which does not generalize, and a wrong resolver silently yields a wrong verdict. Transactional rehearsal is the correct answer and is specified in the v2 section.

## Legal ruleset

Pure deterministic functions over `(AuthorityGrant, ResolvedEffect, WorkflowState)`. No model issues a verdict, ever. The LLM may propose the plan; it never adjudicates and never sees the verdict logic. This is the one rule that cannot bend — the moment a model decides, the determinism claim collapses.

| Rule | Fails when |
| --- | --- |
| `RULE_MATTER_BOUNDARY` | any resolved matter falls outside `grant.matter_id` |
| `RULE_PRIVILEGE_BOUNDARY` | `privileged_count > 0` and `privilege_policy == "exclude"` |
| `RULE_WORKFLOW_BUDGET` | the union of previously committed and proposed consequential document IDs exceeds `grant.max_documents` |
| `RULE_RECIPIENT_BOUNDARY` | any resolved recipient is outside `grant.allowed_recipients` |

The budget counts **unique resources subject to consequential actions across the workflow**, not intermediate reads. Read-only search, metadata inspection, and package-membership preview consume zero budget. For a proposed consequential action, evaluate `len(committed_resource_ids | proposed_resource_ids)` against `grant.max_documents`; repeated IDs within or across steps count once. Previously committed 20 plus 92 disjoint proposed IDs gives 112 → BLOCK; if 12 overlap, the union is 100. Blocked actions and previews consume nothing. Commit the IDs once after successful, verified local execution; retries of the same action do not double-charge. A required but unverifiable postcondition stops the workflow pending reconciliation, rather than allowing further actions against an uncertain budget. Friday v1 implements the union check inside `RULE_WORKFLOW_BUDGET`; persistent cross-workflow commit, retry and reconciliation semantics are post-Friday continuation scope.

**Decision rule:** any FAIL → BLOCK. All PASS → ALLOW, or REQUIRE\_APPROVAL when the action is external or consequential. No weighting, no score, no confidence threshold.

Every failure returns the rule name, the authorized value, the resolved value, and a machine-readable reason.

**Approval is a thin layer above execution, not a core refactor.** Core policy stays binary for v1; read-only search is ALLOW, an external share is REQUIRE\_APPROVAL, a boundary violation is BLOCK. Do not restructure the engine around three-state decisions this week. The REQUIRE\_APPROVAL layer itself is post-Friday continuation scope; Friday v1 stops at the binary verdict.

## Authorization binding and staleness

This is what separates an enforcement boundary from a preview screen, and it is the strongest idea in the design.

**Friday v1 scope:** the action digest, the `AuthorizationRecord`, and failure on digest mismatch. Staleness re-fetch and re-resolve, `ApprovalRecord`, the execution sequence and simulated execution below are post-Friday continuation scope.

**Bind authorization to the exact action previewed.**

```python
canonical = {
    "workflow_id": workflow_id,
    "grant_id": grant_id,
    "grant_digest": sha256(canonical_json(canonical_grant)),
    "plan": canonicalized_plan,
    "resolved_resource_ids": sorted(set(resource_ids)),
    "critical_effect_digest": sha256(canonical_json(critical_effect)),
    "permission_snapshot_digest": permission_snapshot.snapshot_digest,
}
action_digest = sha256(canonical_json(canonical))
```

**Canonical inputs:** `canonical_grant` includes every AuthorityGrant field: grant/workflow identity, matter boundary, document cap, privilege policy, normalized sorted recipient set, grantor, issue time and expiry. A reused `grant_id` must never hide changed authority. `critical_effect` includes the action/tool kind, exact resource membership, per-resource matter IDs and privilege/restriction labels, critical content versions or hashes for documents included in the package, and normalized sorted recipients. Counts alone are insufficient. Use one deterministic canonical JSON encoding, with explicit ordering for set-valued fields and normalized timestamps.

Store an `AuthorizationRecord` with the action digest, its bound snapshots, issue time and expiry. At execution, recompute the digest from current trusted inputs and compare. Mismatch → execution fails.

**Human approval uses the same binding.** Store `ApprovalRecord(action_digest, approved_by, approved_at, expires_at)` after showing the exact resolved action to the human. Execution requires an unexpired approval for that identical `action_digest` when approval is required. Any change to the bound grant, plan, resources, critical effect, or permission snapshot invalidates both authorization and approval; a fresh preview requires fresh approval. The proposing agent cannot issue either record.

This closes a real attack: **preview 12 documents, execute 1,847** under the same approval. An approval that is not bound to a resolved resource set is an approval of a sentence, not an action.

**Staleness (TOCTOU).** State can change between preview and execution — 12 documents at preview, 14 half a second later. For v1, re-fetch the authoritative permission snapshot and re-resolve immediately before execution. Revalidate the user, every resolved resource and the recipient under current permissions; unavailable permission data fails closed and a revoked permission blocks execution. Compare the permission source version and canonical snapshot digest with the authorized snapshot; any change invalidates the authorization and human approval, even if access would still be permitted. Compare the current grant digest, resource IDs, critical labels/content versions and recipients with the bound action. Any difference → `AUTHORIZATION_STALE`, re-authorize and re-approve or block. Never silent continuation. Re-run deterministic policy and the current workflow-budget check before committing; a matching action digest does not override newly consumed budget.

This is the v1-sized version of the concurrency problem. The full answer — versioning, optimistic concurrency, conflict detection — is v2.

**Execution sequence:** load authorization and grant → verify both unexpired → refresh and validate permissions → re-resolve → reconstruct canonical action and verify digest/snapshots → recheck policy and current budget → verify human approval for that same digest if consequential → execute locally → verify postcondition → update budget → append audit event.

**External execution is simulated in v1.** `share_package` creates only a local outbox-like record containing the action digest, exact document IDs, recipients, and a clearly labeled `SIMULATED` status. No email, upload, webhook, or external delivery occurs; there is no dispatcher or background worker. Use the action digest as the local idempotency key so retries do not duplicate the record. Verify actual IDs and recipients from the persisted record, not from the proposed arguments. This demonstrates local execution binding, not successful external delivery.

## Verification

**Post-Friday continuation scope.** Neither identity-level postcondition verification nor the canary scenario ships Friday; the Friday README and video must not imply either exists.

**Write side: identity, not counts.** Compare the exact resource set, not its size.

```python
sha256(sorted(expected_ids)) == sha256(sorted(actual_ids))
```

Twelve documents recorded for simulated sharing is not the same claim as *these* twelve documents recorded for simulated sharing. In v1 the persisted local outbox-like record is the actual effect being verified; verify its recipients and action digest as well as document IDs. Do not retroactively claim the existing ProofGate domains already use identity-level verification if they do not.

**Read side: canaries.** Reading restricted content mutates no state and produces no diff, so the write-side machinery cannot see it. Restricted synthetic documents carry a unique marker:

```
Acquisition codename: BLUE ORCHID 741
```

**Explicit fault injection only:** a separate, clearly labeled synthetic test harness deliberately bypasses the normal context boundary and supplies restricted canary-bearing text to a mock generator or constructed output fixture. This is not normal resolver behavior or a permitted retrieval path. Normal resolver flow never exposes restricted text to the model; verify that separately. The injected case demonstrates detection after a simulated upstream boundary failure.

After generation in that injected scenario, scan the output for canaries belonging to documents outside authorized scope. A hit quarantines the output and emits `RULE_RESTRICTED_CANARY_LEAK`. The check is a string search: deterministic, instant, unarguable.

**Limitation, verbatim in the README:**

> Canary verification is a deterministic demonstration instrument. It can prove leakage in constructed cases where a unique marker appears in output. It does not detect paraphrase, inference, or general semantic leakage. General provenance across generated text remains an open problem.

Never use an LLM as the authoritative leakage judge — that would break the determinism rule for the sake of a better-sounding result. Stating the boundary of your own result precisely is the strongest technical signal in the artifact.

## Audit, trust model, fail-closed

One JSON decision record per governed action, append-only. This is what a compliance reader actually reads, and what makes the demo legible to a non-engineer.

```json
{
  "decision_id": "dec_001",
  "workflow_id": "wf_123",
  "grant_id": "grant_001",
  "authorized": {
    "matter": "M-102", "max_documents": 100,
    "privilege_policy": "exclude",
    "recipients": ["counsel@example.com"]
  },
  "resolved": {
    "document_count": 143,
    "matters_touched": {"M-102": 126, "M-118": 17},
    "privileged_count": 9, "restricted_count": 4,
    "external_recipients": ["counsel@example.com"]
  },
  "checks": {
    "matter_boundary": "FAIL", "privilege_boundary": "FAIL",
    "workflow_budget": "FAIL", "recipient_boundary": "PASS"
  },
  "decision": "BLOCK",
  "executed": false,
  "postcondition": null
}
```

**Trust model.** Trusted: the deterministic resolver, policy engine, budget store, authorization binding, postcondition verifier, audit writer, and consumed permission metadata. Untrusted: the LLM, the agent planner, natural-language extraction, agent-generated arguments, model explanations, repair suggestions. External sources of truth: document store, databases, IAM, ethical-wall provider.

> **Core invariant: the component that proposes an action must never be able to authorize it.**

Audit authorization and execution events also record the action, grant, critical-effect and permission-snapshot digests, permission source version, approval identity and expiry when applicable, and the local simulated execution record ID. Label fault-injection events explicitly. Friday v1 records the authorized-vs-resolved decision, rule checks and action digest; approval, execution and fault-injection fields arrive with their continuation-scope components.

**Fail closed** on every one of: unknown tool, resolver failure, unknown impact, grant missing or expired, budget unavailable, authorization expired, digest mismatch, re-resolve differs from authorization, permission source unavailable or access revoked, permission snapshot changed, required human approval missing/expired or bound to a different digest, postcondition unverifiable for a required action. Friday v1 enforces the cases its components exercise (unknown tool, resolver failure, unknown impact, grant missing or expired, digest mismatch); the rest arrive with their continuation-scope components.

**Rollback is designed, not implemented, in v1.** The record captures what a snapshot would contain; restoration is out of scope and the README says so.

## Demo script

Terminal only, no UI. **Target two minutes, not ninety seconds** — nine beats at ten seconds each is a rushed video, and Scene 5 is the entire point and needs to sit on screen long enough to read.

| # | Beat | What's on screen |
| --- | --- | --- |
| 1 | Human request | The instruction, in plain English. Must look completely reasonable. |
| 2 | Authority grant | Matter, max documents, privilege policy, allowed recipient. |
| 3 | Agent plan | Three tool calls. Still looks fine. |
| 4 | Permission checks | Four PASS lines. Headline: TOOL AND MATTER PERMISSIONS PASS. |
| 5 | Resolved effect | Authorized vs resolved, side by side. **The turn. Hold it.** |
| 6 | Verdict | Three FAIL, one PASS, BLOCK, each naming its boundary. |
| 7 | Tampered execution | Previously valid in-scope authorization, altered resource set → digest mismatch, AUTHORIZATION FAILS. The blocked plan never receives an authorization. |
| 8 | Corrected plan | In-scope, non-privileged, approved recipient → REQUIRE\_APPROVAL → human approves the same action digest. |
| 9 | Simulated local execution | Local outbox-like record only; expected IDs and recipients == persisted IDs and recipients, POSTCONDITION VERIFIED. No external send. |
| 10 | Injected read-side leak | Explicit synthetic fault injection; canary appears in output → OUTPUT QUARANTINED. Normal resolver never exposes restricted text. |

**Friday v1 records beats 1–7.** Beats 8–10 are post-Friday continuation scope and are not shown or claimed in the Friday video or README.

Beats 8 and 9 earn their place in the full-scope video: they prove this is a boundary, not a blocker. Beat 7 is what distinguishes it from a preview screen.

The narrative job of beat 5 is the whole video. Everything before it exists to make the viewer believe nothing is wrong; everything after is consequence.

**Record a rough pass Wednesday night.** Finding problems on Friday with no buffer costs the deadline.

## Schedule, cut order, definition of done

| Day | Deliverable |
| --- | --- |
| Mon 5 Oct | Slice 1: invert registry into the Operations binding, generic policy engine, full existing suite green |
| Tue 6 Oct | Legal models, seeded corpus with related-document links, permission snapshot, multi-step read-only resolver. Regenerate the canonical numbers |
| Wed 7 Oct | `ResolvedEffect`, Legal policy context, four rules, decision record; beats 1–6 end to end in the terminal. Rough video pass at night |
| Thu 8 Oct | Authorization digest, `AuthorizationRecord`, tamper beat 7; README limitations draft |
| Fri 9 Oct | Action Boundary v1: demo-script polish, README, record video (beats 1–7), publish, send |
| After Fri | Continuation scope: staleness handling, persistent budget semantics, REQUIRE\_APPROVAL, simulated execution, identity postcondition, canary scenario, v2 design document |

**The HTTP API is cut from v1.** The demo is terminal output; the video never shows an HTTP call. FastAPI costs a day and buys nothing this week. Keep `resolve`, `authorize` and `execute` as plain Python functions with clean signatures — wrapping them in FastAPI later is an afternoon. The digest binding is the valuable part and needs no server.

**Cut in this order (within Friday v1):** audit formatting, then anything cosmetic. Everything in the continuation scope is already outside Friday.

**Never cut:** the core refactor, the AuthorityGrant, the multi-step resolver, the four deterministic rules, the authorization digest and the tamper-mismatch beat, one killer BLOCK scenario, the existing regression suite. Without those it is a legal-themed script. With them it demonstrates a reusable enforcement architecture.

**Definition of done — Friday v1**

- [ ] Core supports pluggable domain rulesets; registry dependency inverted
- [ ] All existing ProofGate tests still pass; core contains no legal branches
- [ ] Legal domain runs its own authorization-bound pipeline and uses the generic policy engine and shared hashing/audit primitives; no legal-specific branches in the engine, the registry or `guarded_execute`
- [ ] Resolver computes real document sets; side-effecting tools never execute during resolve
- [ ] Bad workflow deterministically BLOCKs
- [ ] Authorization binds canonical grant, plan, resources, critical effect and permission snapshot; digest mismatch fails
- [ ] Budget rule counts unique consequential resource IDs, excluding intermediate reads
- [ ] Normal flow never exposes restricted text to the model
- [ ] Audit records authorized vs resolved
- [ ] Demo runs deterministically from a fresh checkout
- [ ] README states every limitation, and lists continuation scope as not yet implemented
- [ ] Video recorded (beats 1–7)

**Definition of done — continuation scope (post-Friday)**

- [ ] Corrected workflow reaches approval
- [ ] Stale state fails: permissions re-fetched, resources re-resolved
- [ ] Human approval binds the same action digest; permissions and workflow budget revalidated before execution
- [ ] Budget commits unique IDs once after verified execution; retries do not double-charge
- [ ] External execution creates only a local simulated outbox-like record; no external delivery
- [ ] Postcondition verifies actual document IDs, recipients and action digest in the persisted local record
- [ ] Canary demo is labeled fault injection
- [ ] v2 design document

## v2 and claims discipline

### v2: rehearsal, not prediction

A deliverable, not a disclaimer. Knowing precisely why your own v1 is wrong, before a reviewer tells you, is the strongest signal in the artifact.

**The flaw:** v1 predicts impact through per-tool resolvers. Every new tool needs one, and a wrong resolver silently produces a wrong verdict. Prediction is the weak form.

**The right architecture:** run the workflow against a private isolated copy, observe the actual diff plus the queued external effects, apply policy to that real diff, then commit atomically or discard. Rollback becomes trivial because nothing touches real data until approval.

Three hard problems it forces, all on data-platform ground:

1. **Safe commit under concurrency.** Data may change between rehearsal and approval. Resource versioning, optimistic concurrency, conflict detection, re-run on stale state. Never silently apply a rehearsal computed on stale state.
2. **Irreversible external effects.** Email and webhooks cannot be rehearsed. Queue during rehearsal, dispatch exactly once after authorization — a transactional outbox applied to agent workflows.
3. **Read-side information flow.** Rehearsal sees writes, not knowledge moving through reads. Document labels → agent context accumulates them → outputs inherit them → recipient policy validates output labels.

**The known tension in (3):** conservative label propagation is monotonic and eventually taints everything, blocking all useful work. A real system needs declassification or stronger context isolation. There is existing academic work on information-flow control for agents — cite it rather than claiming the idea.

This is a quarter of work, not a week. It ships after v1, as the second post.

### Claims discipline

**Never claim** that any named commercial product lacks this capability, that the system solves semantic leakage, that resolver prediction equals transactional execution, that both domains share one execution pipeline, that continuation-scope behavior (approval, execution, postcondition verification, staleness handling, canary detection) exists before it ships, that this is production-ready, or that the synthetic domain models real law-firm governance.

**Safe framing:**

> A standalone reference implementation exploring a general runtime question: once an agent is authorized to operate in a domain, how does the system verify that the action it is about to take remains inside the human's granted authority?

Commercial legal AI is closed — its internals are not observable from outside, and a claim about what it lacks is unsupportable and made to people who know the answer. If a company already solved this internally, the artifact still demonstrates independent systems thinking at the right layer.

**Attribution:** standalone, on synthetic data. Not built on top of any product; no commercial API was used or accessible. Public documentation informed the domain model, and the README says exactly that.

**Naming:** give it a name of its own. Relevance to any particular company should be obvious to a reader without being asserted by the author.
