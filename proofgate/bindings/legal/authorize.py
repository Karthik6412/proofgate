"""Friday v1 Legal authorization entry point. In order: validate the
grant, require the permission state, resolve the plan, run the coarse
permission checks, evaluate the four rules -- and nothing more.

Grant validity is a precondition, not a rule: with no authority in effect
at `now` there is nothing to compare the effect against, so a missing,
not-yet-valid or expired grant fails closed before the plan is resolved.
Resolver failure means the concrete impact is unknown, so it fails closed
through the generic unknown_impact_decision. Neither enters LEGAL_RULESET.

Permission is checked separately from authority. A missing permission
snapshot or agent capability set, or any failed coarse check, fails closed
as PERMISSION_DENIED before the rules run. Restricted consequential
documents do not stop rule evaluation -- the canonical blocked workflow
still reaches its rule verdict -- but they veto ALLOW.

`now` is always supplied by the caller; this module never reads a clock.
The result is a transient record of one authorization attempt, not an
AuthorizationRecord.
"""

import datetime
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Mapping

from legal.grants import GrantValidity, grant_validity
from legal.models import AgentCapabilities, AuthorityGrant, Document, PermissionSnapshot
from proofgate.bindings.legal.permission_checks import PermissionCheckResult, check_permissions
from proofgate.bindings.legal.plan import LegalPlan
from proofgate.bindings.legal.resolver import LegalResolvedEffect, ResolutionError, resolve_plan
from proofgate.bindings.legal.rules import evaluate_legal_rules
from proofgate.policy import PolicyDecision, unknown_impact_decision


class LegalAuthorizationStatus(str, Enum):
    GRANT_MISSING = "GRANT_MISSING"
    GRANT_NOT_YET_VALID = "GRANT_NOT_YET_VALID"
    GRANT_EXPIRED = "GRANT_EXPIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RESOLUTION_FAILED = "RESOLUTION_FAILED"
    EVALUATED = "EVALUATED"


_GRANT_FAILURES = {
    GrantValidity.MISSING: LegalAuthorizationStatus.GRANT_MISSING,
    GrantValidity.NOT_YET_VALID: LegalAuthorizationStatus.GRANT_NOT_YET_VALID,
    GrantValidity.EXPIRED: LegalAuthorizationStatus.GRANT_EXPIRED,
}


@dataclass(frozen=True)
class LegalAuthorizationAttempt:
    """status says how far authorization got. decision is None when no
    valid grant existed; for RESOLUTION_FAILED it is the generic
    unknown-impact decision; for EVALUATED it is the four-rule decision.
    effect is present only when resolution succeeded. permissions holds the
    coarse check results whenever they were computed.

    ALLOW requires all of: the rules evaluated, the rule decision is ALLOW,
    and no consequential document is restricted."""

    status: LegalAuthorizationStatus
    decision: PolicyDecision | None
    effect: LegalResolvedEffect | None
    reason: str | None
    permissions: PermissionCheckResult | None = None

    @property
    def verdict(self) -> Literal["ALLOW", "BLOCK"]:
        if (
            self.status is LegalAuthorizationStatus.EVALUATED
            and self.decision.verdict == "ALLOW"
            and not self.permissions.restricted_resource_ids
        ):
            return "ALLOW"
        return "BLOCK"


def authorize_legal_plan(
    grant: AuthorityGrant | None,
    plan: LegalPlan,
    *,
    now: datetime.datetime,
    permission_snapshot: PermissionSnapshot | None,
    agent_capabilities: AgentCapabilities | None,
    committed_resource_ids: tuple[str, ...] = (),
    documents: Mapping[str, Document] | None = None,
) -> LegalAuthorizationAttempt:
    validity = grant_validity(grant, now)
    if validity is not GrantValidity.VALID:
        status = _GRANT_FAILURES[validity]
        return LegalAuthorizationAttempt(status=status, decision=None, effect=None, reason=status.value.lower())

    for state, reason in (
        (permission_snapshot, "permission_snapshot_missing"),
        (agent_capabilities, "agent_capabilities_missing"),
    ):
        if state is None:
            return LegalAuthorizationAttempt(
                status=LegalAuthorizationStatus.PERMISSION_DENIED, decision=None, effect=None, reason=reason
            )

    try:
        effect = resolve_plan(plan, documents=documents)
    except ResolutionError as error:
        return LegalAuthorizationAttempt(
            status=LegalAuthorizationStatus.RESOLUTION_FAILED,
            decision=unknown_impact_decision(
                f"The plan could not be resolved to a known effect: {error}",
                "a plan that resolves to a known effect",
            ),
            effect=None,
            reason=str(error),
        )

    permissions = check_permissions(permission_snapshot, agent_capabilities, plan, effect)
    if not permissions.coarse_checks_pass:
        return LegalAuthorizationAttempt(
            status=LegalAuthorizationStatus.PERMISSION_DENIED,
            decision=None,
            effect=effect,
            reason="permission_check_failed",
            permissions=permissions,
        )

    return LegalAuthorizationAttempt(
        status=LegalAuthorizationStatus.EVALUATED,
        decision=evaluate_legal_rules(grant, effect, committed_resource_ids),
        effect=effect,
        reason=None,
        permissions=permissions,
    )
