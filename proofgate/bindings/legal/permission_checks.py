"""Beat 4: the coarse permission checks, plus the document-restriction veto.

Permission is not authorization. These checks ask whether the user and
agent may do this at all -- against the trusted PermissionSnapshot and the
seeded AgentCapabilities -- independently of what the human's grant
authorizes, which the four Legal rules decide. They read only the already
resolved effect and the plan; nothing is re-fetched or re-resolved.

The four checks, in the spec's order:

    user_matter_access   every matter the resolved effect touched, reads
                         included, is in permitted_matters
    agent_may_search     every search tool the plan uses is an allowed tool
    agent_may_package    every package tool the plan uses is an allowed tool
    recipient_permitted  every consequential recipient is in
                         permitted_recipients

Document-level restrictions are not waived by those checks. They are a
separate veto: consequential documents listed in the snapshot's
restricted_document_ids. Reading restricted metadata during resolution is
allowed; acting on a restricted document is not.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from legal.models import AgentCapabilities, PermissionSnapshot
from proofgate.bindings.legal.plan import LegalPlan
from proofgate.bindings.legal.resolver import LegalResolvedEffect

SEARCH_TOOLS = ("search_documents",)
PACKAGE_TOOLS = ("compile_package",)


class PermissionCheck(BaseModel):
    """One coarse check: what is permitted, what was required, and what
    was required but not permitted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: Literal["user_matter_access", "agent_may_search", "agent_may_package", "recipient_permitted"]
    status: Literal["PASS", "FAIL"]
    permitted: tuple[str, ...]
    required: tuple[str, ...]
    denied: tuple[str, ...]


class PermissionCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    checks: tuple[PermissionCheck, ...]
    restricted_resource_ids: tuple[str, ...]

    @property
    def coarse_checks_pass(self) -> bool:
        return all(check.status == "PASS" for check in self.checks)


def _check(check_id: str, permitted: frozenset[str], required: set[str]) -> PermissionCheck:
    denied = tuple(sorted(required - permitted))
    return PermissionCheck(
        check_id=check_id,
        status="FAIL" if denied else "PASS",
        permitted=tuple(sorted(permitted)),
        required=tuple(sorted(required)),
        denied=denied,
    )


def check_permissions(
    snapshot: PermissionSnapshot,
    capabilities: AgentCapabilities,
    plan: LegalPlan,
    effect: LegalResolvedEffect,
) -> PermissionCheckResult:
    plan_tools = {step.tool for step in plan.steps}
    resolved_matters = {matter for trace in effect.steps for matter, _ in trace.matters_touched}
    return PermissionCheckResult(
        checks=(
            _check("user_matter_access", snapshot.permitted_matters, resolved_matters),
            _check("agent_may_search", capabilities.allowed_tools, plan_tools & set(SEARCH_TOOLS)),
            _check("agent_may_package", capabilities.allowed_tools, plan_tools & set(PACKAGE_TOOLS)),
            _check(
                "recipient_permitted",
                snapshot.permitted_recipients,
                set(effect.dimensions.external_recipients),
            ),
        ),
        restricted_resource_ids=tuple(
            sorted(set(effect.consequential_resource_ids) & snapshot.restricted_document_ids)
        ),
    )
