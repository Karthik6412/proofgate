"""Friday v1 Legal decision record: one append-only JSON record per governed
action that reached policy evaluation.

The JSON follows the spec's sample -- decision_id, workflow_id, grant_id,
authorized, resolved, checks, decision, executed, postcondition -- plus the
two things the spec says Friday v1 also records: the action digest and the
rule checks' machine-readable detail (check_details).

Every field is copied from an object that already holds it: authorized
from the AuthorityGrant, resolved from the LegalResolvedEffect, checks and
check_details from the PolicyDecision's outcomes, decision from the
authorization attempt, and the action digest from the AuthorizationRecord
(ALLOW) or the shared action_digest of the same LegalAction (BLOCK).
Nothing is re-resolved or re-evaluated here.

Attempts that never reached policy evaluation (missing, not-yet-valid or
expired grant; resolution failure) get no decision record: the spec does
not define one, and the authorization attempt already carries them.
executed is always false and postcondition always null in Friday v1.
"""

import datetime
import uuid
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, field_serializer

from legal.models import AuthorityGrant, Document, PermissionSnapshot
from proofgate.audit import append_jsonl
from proofgate.bindings.legal.authorization import (
    AuthorizationRecord,
    LegalAction,
    action_digest,
    authorize_and_bind,
)
from proofgate.bindings.legal.authorize import LegalAuthorizationAttempt, LegalAuthorizationStatus
from proofgate.bindings.legal.plan import LegalPlan
from proofgate.bindings.legal.rules import (
    RULE_MATTER_BOUNDARY,
    RULE_PRIVILEGE_BOUNDARY,
    RULE_RECIPIENT_BOUNDARY,
)
from proofgate.policy import RULE_WORKFLOW_BUDGET

DEFAULT_DECISION_LOG_PATH = Path("artifacts/legal_decisions.jsonl")

CHECK_KEYS = {
    RULE_MATTER_BOUNDARY: "matter_boundary",
    RULE_PRIVILEGE_BOUNDARY: "privilege_boundary",
    RULE_WORKFLOW_BUDGET: "workflow_budget",
    RULE_RECIPIENT_BOUNDARY: "recipient_boundary",
}

CheckStatus = Literal["PASS", "FAIL"]


class AuthorizedSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    matter: str
    max_documents: int
    privilege_policy: Literal["exclude", "include"]
    recipients: tuple[str, ...]


class ResolvedSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_count: int
    matters_touched: tuple[tuple[str, int], ...]
    privileged_count: int
    restricted_count: int
    external_recipients: tuple[str, ...]

    @field_serializer("matters_touched")
    def _matters_as_object(self, value: tuple[tuple[str, int], ...]) -> dict[str, int]:
        return dict(value)


class ChecksSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    matter_boundary: CheckStatus
    privilege_boundary: CheckStatus
    workflow_budget: CheckStatus
    recipient_boundary: CheckStatus


class LegalDecisionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str
    workflow_id: str
    grant_id: str
    authorized: AuthorizedSection
    resolved: ResolvedSection
    checks: ChecksSection
    check_details: tuple[tuple[str, tuple[tuple[str, Any], ...]], ...]
    action_digest: str
    decision: Literal["ALLOW", "BLOCK"]
    executed: Literal[False] = False
    postcondition: None = None

    @field_serializer("check_details")
    def _details_as_object(self, value: tuple[tuple[str, tuple[tuple[str, Any], ...]], ...]) -> dict:
        return {key: dict(detail) for key, detail in value}


def new_decision_id() -> str:
    """Server-side decision-record ID, in the same style as audit event IDs."""
    return f"dec-{uuid.uuid4().hex}"


def build_decision_record(
    grant: AuthorityGrant,
    attempt: LegalAuthorizationAttempt,
    digest: str,
    *,
    decision_id: str | None = None,
) -> LegalDecisionRecord:
    """Assemble the record from objects that already hold every value. Pure:
    no filesystem, no resolution, no rule evaluation."""
    if attempt.status is not LegalAuthorizationStatus.EVALUATED:
        raise ValueError(f"No decision record for an attempt that was not evaluated ({attempt.status.value}).")
    outcomes = attempt.decision.outcomes
    if sorted(outcome.rule_id for outcome in outcomes) != sorted(CHECK_KEYS):
        raise ValueError(f"Unexpected rule outcomes {[outcome.rule_id for outcome in outcomes]}.")

    effect = attempt.effect
    statuses = {CHECK_KEYS[outcome.rule_id]: "FAIL" if outcome.triggered else "PASS" for outcome in outcomes}
    details = tuple(
        (
            CHECK_KEYS[outcome.rule_id],
            tuple(
                {
                    "rule_id": outcome.rule_id,
                    "explanation": outcome.triggered_rule.explanation if outcome.triggered_rule else None,
                    **dict(outcome.details or {}),
                }.items()
            ),
        )
        for outcome in outcomes
    )
    return LegalDecisionRecord(
        decision_id=decision_id or new_decision_id(),
        workflow_id=grant.workflow_id,
        grant_id=grant.grant_id,
        authorized=AuthorizedSection(
            matter=grant.matter_id,
            max_documents=grant.max_documents,
            privilege_policy=grant.privilege_policy,
            recipients=grant.allowed_recipients,
        ),
        resolved=ResolvedSection(
            document_count=effect.total_affected,
            matters_touched=effect.dimensions.matters_touched,
            privileged_count=effect.dimensions.privileged_count,
            restricted_count=effect.dimensions.restricted_count,
            external_recipients=effect.dimensions.external_recipients,
        ),
        checks=ChecksSection(**statuses),
        check_details=details,
        action_digest=digest,
        decision=attempt.verdict,
    )


def append_decision_record(record: LegalDecisionRecord, path: Path | None = None) -> None:
    """Append one JSON line; never overwrites. DEFAULT_DECISION_LOG_PATH is
    read at call time and is separate from the Operations audit log."""
    append_jsonl(record, path if path is not None else DEFAULT_DECISION_LOG_PATH)


def authorize_and_record(
    grant: AuthorityGrant | None,
    plan: LegalPlan,
    *,
    now: datetime.datetime,
    permission_snapshot: PermissionSnapshot,
    committed_resource_ids: tuple[str, ...] = (),
    documents: Mapping[str, Document] | None = None,
    decision_log_path: Path | None = None,
) -> tuple[LegalAuthorizationAttempt, AuthorizationRecord | None, LegalDecisionRecord | None]:
    """Authorize and bind (unchanged), then record an evaluated decision.

    ALLOW: the record's digest is the AuthorizationRecord's. BLOCK: no
    AuthorizationRecord exists; the digest of the same evaluated action is
    computed with the shared action_digest so the record still names the
    exact action. Not evaluated: no decision record.
    """
    attempt, authorization = authorize_and_bind(
        grant,
        plan,
        now=now,
        permission_snapshot=permission_snapshot,
        committed_resource_ids=committed_resource_ids,
        documents=documents,
    )
    if attempt.status is not LegalAuthorizationStatus.EVALUATED:
        return attempt, None, None

    if authorization is not None:
        digest = authorization.action_digest
    else:
        digest = action_digest(
            LegalAction(grant=grant, plan=plan, effect=attempt.effect, permission_snapshot=permission_snapshot)
        )
    record = build_decision_record(grant, attempt, digest)
    append_decision_record(record, decision_log_path)
    return attempt, authorization, record
