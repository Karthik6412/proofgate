"""ProofGate Action Boundary v1: deterministic terminal demo, beats 1-7.

Follows the spec's demo script exactly: human request, authority grant,
agent plan, permission checks, resolved effect, verdict, tampered
execution. Every fact and verdict printed comes from the real production
code -- seeded corpus and providers, resolver, coarse permission checks,
the four Legal rules, authorization binding, decision record and digest
verification. This script only orchestrates and formats.

Fully local and deterministic: no network, no model, no API key. Decision
records are written to a temporary directory, so rehearsals never touch
artifacts/. Nothing is executed or sent -- v1 stops at authorization.

Run it from the repository root with:

    source .venv/bin/activate
    python scripts/legal_demo.py
"""

from __future__ import annotations

import datetime
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from legal.corpus import documents_by_id  # noqa: E402
from legal.models import AgentCapabilities, AuthorityGrant, PermissionSnapshot  # noqa: E402
from legal.permissions import (  # noqa: E402
    SEEDED_AGENT_ID,
    SEEDED_USER_ID,
    get_agent_capabilities,
    get_permission_snapshot,
)
from proofgate.bindings.legal.authorization import (  # noqa: E402
    AuthorizationDigestMismatch,
    AuthorizationRecord,
    LegalAction,
    verify_authorization,
)
from proofgate.bindings.legal.authorize import LegalAuthorizationAttempt  # noqa: E402
from proofgate.bindings.legal.decision_record import (  # noqa: E402
    CHECK_KEYS,
    LegalDecisionRecord,
    authorize_and_record,
)
from proofgate.bindings.legal.plan import CANONICAL_PLAN, CLEAN_PLAN, LegalPlan  # noqa: E402
from proofgate.bindings.legal.resolver import ResourceFacts  # noqa: E402

INSTRUCTION = (
    "Prepare the diligence package for Matter M-102 and share the\n"
    "non-privileged documents with outside counsel."
)
GRANTED_AT = datetime.datetime(2026, 10, 9, 9, 0, tzinfo=datetime.timezone.utc)
NOW = GRANTED_AT + datetime.timedelta(hours=1)
SLIPPED_IN_DOC = "D-118-001"  # the document added to the package after authorization

CHECK_LABELS = {
    "user_matter_access": "User may access matters",
    "agent_may_search": "Agent may search",
    "agent_may_package": "Agent may package",
    "recipient_permitted": "Recipient permitted",
}


def demo_grant() -> AuthorityGrant:
    """What the human authorized, per the spec's canonical scenario."""
    return AuthorityGrant(
        grant_id="grant_001",
        workflow_id="wf_123",
        matter_id="M-102",
        max_documents=100,
        privilege_policy="exclude",
        allowed_recipients=("counsel@example.com",),
        granted_by="partner-01",
        granted_at=GRANTED_AT,
        expires_at=GRANTED_AT + datetime.timedelta(hours=8),
    )


@dataclass(frozen=True)
class TamperResult:
    authorization: AuthorizationRecord | None
    untouched_verified: bool
    tampered_action: LegalAction | None
    mismatch: AuthorizationDigestMismatch | None


@dataclass(frozen=True)
class DemoResult:
    grant: AuthorityGrant
    plan: LegalPlan
    canonical: LegalAuthorizationAttempt
    canonical_authorization: AuthorizationRecord | None
    canonical_record: LegalDecisionRecord | None
    clean: LegalAuthorizationAttempt
    tamper: TamperResult


def _tamper(
    grant: AuthorityGrant,
    attempt: LegalAuthorizationAttempt,
    authorization: AuthorizationRecord | None,
    snapshot: PermissionSnapshot,
) -> TamperResult:
    """Verify the authorized action, then present it again with one extra
    document slipped into the package, and verify that."""
    if authorization is None:
        return TamperResult(None, False, None, None)

    action = LegalAction(grant=grant, plan=CLEAN_PLAN, effect=attempt.effect, permission_snapshot=snapshot)
    verify_authorization(authorization, action)

    document = documents_by_id()[SLIPPED_IN_DOC]
    slipped_in = ResourceFacts(
        doc_id=document.doc_id,
        matter_id=document.matter_id,
        privileged=document.privileged,
        restricted=document.restricted,
        content_hash=document.content_hash,
    )
    resources = tuple(sorted(attempt.effect.consequential_resources + (slipped_in,), key=lambda r: r.doc_id))
    tampered = action.model_copy(
        update={
            "effect": attempt.effect.model_copy(
                update={
                    "consequential_resources": resources,
                    "consequential_resource_ids": tuple(resource.doc_id for resource in resources),
                    "total_affected": len(resources),
                }
            )
        }
    )
    try:
        verify_authorization(authorization, tampered)
    except AuthorizationDigestMismatch as mismatch:
        return TamperResult(authorization, True, tampered, mismatch)
    return TamperResult(authorization, True, tampered, None)


def run_demo(
    decision_log_path: Path,
    *,
    permission_snapshot: PermissionSnapshot | None = None,
    agent_capabilities: AgentCapabilities | None = None,
) -> DemoResult:
    """Run the canonical and clean workflows through the real flow.
    Snapshot and capabilities default to the seeded providers."""
    grant = demo_grant()
    snapshot = permission_snapshot or get_permission_snapshot(SEEDED_USER_ID, captured_at=GRANTED_AT)
    capabilities = agent_capabilities or get_agent_capabilities(SEEDED_AGENT_ID)

    def authorize(plan: LegalPlan):
        return authorize_and_record(
            grant,
            plan,
            now=NOW,
            permission_snapshot=snapshot,
            agent_capabilities=capabilities,
            decision_log_path=decision_log_path,
        )

    canonical, canonical_authorization, canonical_record = authorize(CANONICAL_PLAN)
    clean, clean_authorization, _ = authorize(CLEAN_PLAN)
    return DemoResult(
        grant=grant,
        plan=CANONICAL_PLAN,
        canonical=canonical,
        canonical_authorization=canonical_authorization,
        canonical_record=canonical_record,
        clean=clean,
        tamper=_tamper(grant, clean, clean_authorization, snapshot),
    )


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def _beat(number: int, title: str) -> list[str]:
    return ["", f"BEAT {number} -- {title}", "-" * 64]


def _short(digest: str) -> str:
    return f"{digest[:16]}..."


def _call(step) -> str:
    arguments = ", ".join(
        f"{name}={value!r}" if isinstance(value, str) else f"{name}={value}"
        for name, value in step.argument_map().items()
    )
    return f"{step.tool}({arguments})"


def _matters(pairs) -> str:
    return ", ".join(f"{matter}: {count}" for matter, count in pairs)


def render(result: DemoResult) -> list[str]:
    grant, attempt = result.grant, result.canonical
    lines = ["ProofGate Action Boundary v1 -- Legal demo (synthetic data)", "=" * 64]

    lines += _beat(1, "Human request")
    lines += [f"  {line}" for line in INSTRUCTION.splitlines()]

    lines += _beat(2, "Authority grant")
    lines += [
        f"  Matter:             {grant.matter_id}",
        f"  Max documents:      {grant.max_documents}",
        f"  Privilege policy:   {grant.privilege_policy}",
        f"  Allowed recipients: {', '.join(grant.allowed_recipients)}",
        f"  Granted by {grant.granted_by}, valid until {grant.expires_at:%Y-%m-%d %H:%M} UTC",
    ]

    lines += _beat(3, "Agent plan")
    lines += [f"  {index}. {_call(step)}" for index, step in enumerate(result.plan.steps, start=1)]

    lines += _beat(4, "Permission checks")
    if attempt.permissions is None:
        lines += [f"  PERMISSION STATE UNAVAILABLE ({attempt.reason})"]
    else:
        for check in attempt.permissions.checks:
            shown = ", ".join(check.denied or check.required) or "(none required)"
            lines.append(f"  {check.status}  {CHECK_LABELS[check.check_id]:<24} {shown}")
        if attempt.permissions.coarse_checks_pass:
            lines.append("  TOOL AND MATTER PERMISSIONS PASS")
        else:
            failed = [CHECK_LABELS[check.check_id] for check in attempt.permissions.checks if check.status == "FAIL"]
            lines.append(f"  PERMISSION CHECK FAILED: {', '.join(failed)}")
        lines.append("  (Coarse checks do not waive document-level restrictions.)")

    lines += _beat(5, "Resolved effect -- authorized vs resolved")
    if attempt.effect is None:
        lines.append(f"  Not resolved ({attempt.status.value}).")
    else:
        effect, dims = attempt.effect, attempt.effect.dimensions
        expansion = next((trace for trace in effect.steps if trace.tool == "compile_package"), None)
        if expansion is not None:
            lines.append(
                f"  The plan names {', '.join(effect.requested_scope.matters)}. Related-document expansion "
                f"added {len(expansion.added_by_expansion)} documents."
            )
        privileged_allowed = "0 (privilege excluded)" if grant.privilege_policy == "exclude" else "any"
        rows = [
            ("", "AUTHORIZED", "RESOLVED"),
            ("Matters", grant.matter_id, _matters(dims.matters_touched)),
            ("Documents", f"<= {grant.max_documents}", str(effect.total_affected)),
            ("Privileged", privileged_allowed, str(dims.privileged_count)),
            ("Recipients", ", ".join(grant.allowed_recipients), ", ".join(dims.external_recipients)),
        ]
        lines += [f"  {label:<12}{left:<26}{right}" for label, left, right in rows]
        if attempt.permissions is not None:
            restricted = attempt.permissions.restricted_resource_ids
            lines.append(
                f"  Restricted (ethical wall): {len(restricted)} consequential document(s) -- "
                "blocks authorization on its own."
            )

    lines += _beat(6, "Verdict")
    if attempt.decision is None or attempt.permissions is None:
        lines.append(f"  Authority rules not evaluated ({attempt.status.value}: {attempt.reason}).")
    else:
        lines.append("  Permission:  PASS (all four coarse checks)")
        lines.append("  Authority / Legal boundaries:")
        for outcome in attempt.decision.outcomes:
            status = "FAIL" if outcome.triggered else "PASS"
            line = f"    {status}  {CHECK_KEYS[outcome.rule_id]}"
            if outcome.triggered:
                line = f"{line:<30} {outcome.triggered_rule.explanation}"
            lines.append(line)
    lines.append(f"  DECISION: {attempt.verdict}")
    lines.append(
        "  No authorization issued."
        if result.canonical_authorization is None
        else f"  Authorization issued: {_short(result.canonical_authorization.action_digest)}"
    )
    if result.canonical_record is not None:
        lines.append(
            f"  Decision record appended: decision={result.canonical_record.decision}, "
            f"action digest {_short(result.canonical_record.action_digest)}"
        )

    lines += _beat(7, "Tampered execution")
    tamper = result.tamper
    lines.append("  An in-scope plan (same request, no related-document expansion):")
    lines.append(
        f"    {result.clean.verdict}: {result.clean.effect.total_affected if result.clean.effect else 0} "
        f"documents, matters {_matters(result.clean.effect.dimensions.matters_touched) if result.clean.effect else '-'}"
    )
    if tamper.authorization is None:
        lines.append("  No authorization was issued, so there is nothing to tamper with.")
    else:
        authorized_count = result.clean.effect.total_affected
        lines += [
            f"    AuthorizationRecord issued, action digest {_short(tamper.authorization.action_digest)}",
            f"    Untouched action verifies: {'yes' if tamper.untouched_verified else 'NO'}",
            f"  After authorization, the package is altered: + {SLIPPED_IN_DOC} "
            f"({documents_by_id()[SLIPPED_IN_DOC].matter_id}), "
            f"{authorized_count} -> {tamper.tampered_action.effect.total_affected} documents.",
        ]
        if tamper.mismatch is not None:
            lines += [
                f"    authorized digest: {_short(tamper.mismatch.expected)}",
                f"    presented digest:  {_short(tamper.mismatch.actual)}",
                "  DIGEST MISMATCH -- AUTHORIZATION FAILS",
            ]
        else:
            lines.append("  UNEXPECTED: the altered action verified")
    lines.append(
        "  The blocked plan never received an authorization."
        if result.canonical_authorization is None
        else "  UNEXPECTED: the blocked plan received an authorization."
    )

    lines += ["", "=" * 64, "Nothing was executed or sent: v1 stops at authorization."]
    return lines


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="proofgate-legal-demo-") as directory:
        result = run_demo(Path(directory) / "legal_decisions.jsonl")
    print("\n".join(render(result)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
