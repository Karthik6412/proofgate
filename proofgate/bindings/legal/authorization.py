"""Bind an authorization to the exact action that was authorized.

L3 answers whether an action is authorized. This module answers whether
the action presented now is exactly the one that was: an ALLOW mints an
AuthorizationRecord carrying the action digest, and verification
recomputes the digest from the presented action and rejects any
difference. Verification never re-authorizes.

What the action digest binds (spec: "Bind authorization to the exact
action previewed"):

    workflow_id, grant_id      which workflow and grant this is for
    grant_digest               every AuthorityGrant field, recipients
                               sorted, timestamps normalized to UTC ISO-8601,
                               so a reused grant_id cannot hide changed
                               authority
    plan                       every step, in order: step_id, tool and all
                               arguments (the requested operation and its
                               targets)
    resolved_resource_ids      the consequential document IDs, sorted
    critical_effect_digest     the consequential action kinds, each
                               consequential document's matter, privilege
                               and restriction labels and content hash, and
                               the sorted recipients -- identities, not
                               counts
    permission_snapshot_digest the snapshot's own digest (which already
                               excludes captured_at)

What it deliberately excludes: documents only read during resolution and
the per-step trace (the plan and consequential effect already capture the
action), the policy decision and its details (a result, not the action),
committed_resource_ids (workflow budget state, re-checked by policy rather
than frozen into the action), the record's issued_at, and any document
text.

Friday v1 verification compares the presented action as given; re-fetching
permissions and re-resolving before execution is continuation scope.
"""

import datetime
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict

from legal.models import AuthorityGrant, Document, PermissionSnapshot
from proofgate.bindings.legal.authorize import LegalAuthorizationAttempt, authorize_legal_plan
from proofgate.bindings.legal.plan import LegalPlan
from proofgate.bindings.legal.resolver import LegalResolvedEffect
from proofgate.selector import sha256_digest


class LegalAction(BaseModel):
    """Everything an authorization is bound to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    grant: AuthorityGrant
    plan: LegalPlan
    effect: LegalResolvedEffect
    permission_snapshot: PermissionSnapshot


class AuthorizationRecord(BaseModel):
    """The fact that one exact action was authorized. Only an ALLOW can
    produce one. expires_at is the grant's own expiry -- no second TTL --
    and is stored, not enforced: checking it belongs to the execution
    sequence, which is continuation scope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_digest: str
    workflow_id: str
    grant_id: str
    grant_digest: str
    permission_snapshot_digest: str
    verdict: Literal["ALLOW"]
    evaluated_rule_ids: tuple[str, ...]
    issued_at: datetime.datetime
    expires_at: datetime.datetime


class AuthorizationDigestMismatch(Exception):
    """The presented action is not the action that was authorized."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"Action digest {actual} does not match authorized digest {expected}.")
        self.expected = expected
        self.actual = actual


def _utc(value: datetime.datetime) -> str:
    return value.astimezone(datetime.timezone.utc).isoformat()


def canonical_grant(grant: AuthorityGrant) -> dict:
    return {
        "grant_id": grant.grant_id,
        "workflow_id": grant.workflow_id,
        "matter_id": grant.matter_id,
        "max_documents": grant.max_documents,
        "privilege_policy": grant.privilege_policy,
        "allowed_recipients": sorted(grant.allowed_recipients),
        "granted_by": grant.granted_by,
        "granted_at": _utc(grant.granted_at),
        "expires_at": _utc(grant.expires_at),
    }


def canonical_plan(plan: LegalPlan) -> list[dict]:
    return [
        {"step_id": step.step_id, "tool": step.tool, "arguments": dict(step.arguments)} for step in plan.steps
    ]


def critical_effect(effect: LegalResolvedEffect) -> dict:
    consequential_steps = {
        trace.tool for trace in effect.steps if trace.recipient is not None
    }
    return {
        "actions": sorted(consequential_steps),
        "resources": [
            {
                "doc_id": resource.doc_id,
                "matter_id": resource.matter_id,
                "privileged": resource.privileged,
                "restricted": resource.restricted,
                "content_hash": resource.content_hash,
            }
            for resource in sorted(effect.consequential_resources, key=lambda resource: resource.doc_id)
        ],
        "recipients": sorted(effect.dimensions.external_recipients),
    }


def canonical_action(action: LegalAction) -> dict:
    return {
        "workflow_id": action.grant.workflow_id,
        "grant_id": action.grant.grant_id,
        "grant_digest": sha256_digest(canonical_grant(action.grant)),
        "plan": canonical_plan(action.plan),
        "resolved_resource_ids": sorted(set(action.effect.consequential_resource_ids)),
        "critical_effect_digest": sha256_digest(critical_effect(action.effect)),
        "permission_snapshot_digest": action.permission_snapshot.snapshot_digest,
    }


def action_digest(action: LegalAction) -> str:
    return sha256_digest(canonical_action(action))


def authorize_and_bind(
    grant: AuthorityGrant | None,
    plan: LegalPlan,
    *,
    now: datetime.datetime,
    permission_snapshot: PermissionSnapshot,
    committed_resource_ids: tuple[str, ...] = (),
    documents: Mapping[str, Document] | None = None,
) -> tuple[LegalAuthorizationAttempt, AuthorizationRecord | None]:
    """Authorize with L3 unchanged; on ALLOW, mint a record bound to the
    very grant, plan and resolved effect that were just authorized. Any
    other outcome yields no record."""
    attempt = authorize_legal_plan(
        grant, plan, now=now, committed_resource_ids=committed_resource_ids, documents=documents
    )
    if attempt.verdict != "ALLOW":
        return attempt, None

    action = LegalAction(grant=grant, plan=plan, effect=attempt.effect, permission_snapshot=permission_snapshot)
    canonical = canonical_action(action)
    record = AuthorizationRecord(
        action_digest=sha256_digest(canonical),
        workflow_id=canonical["workflow_id"],
        grant_id=canonical["grant_id"],
        grant_digest=canonical["grant_digest"],
        permission_snapshot_digest=canonical["permission_snapshot_digest"],
        verdict="ALLOW",
        evaluated_rule_ids=tuple(outcome.rule_id for outcome in attempt.decision.outcomes),
        issued_at=now,
        expires_at=grant.expires_at,
    )
    return attempt, record


def verify_authorization(record: AuthorizationRecord, action: LegalAction) -> None:
    """Pass silently if the presented action is exactly the authorized one;
    raise AuthorizationDigestMismatch otherwise. Never re-authorizes."""
    actual = action_digest(action)
    if actual != record.action_digest:
        raise AuthorizationDigestMismatch(expected=record.action_digest, actual=actual)
