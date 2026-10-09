"""The four Friday v1 Legal Action Boundary rules.

Each rule compares the resolved effect's CONSEQUENTIAL resources -- what
the share would actually act on -- against the human's AuthorityGrant,
through the generic proofgate.policy engine. Resources that were only read
during resolution never count, and nothing here edits, filters or
re-resolves the effect: an out-of-scope resource is reported, never hidden.

Every outcome carries a flat, read-only details mapping: "authorized",
"resolved" and "reason" (a machine-readable code, None on PASS) on every
rule, plus rule-specific evidence keys. Values are only strings, ints,
None and tuples, so `canonical_json(dict(details))` serializes them
deterministically.

Grant validity (missing or expired) is not one of these rules and is not
checked here: evaluate_legal_rules assumes the caller already holds a
valid grant. proofgate.bindings.legal.authorize.authorize_legal_plan is
the entry point that checks it first.
"""

from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, field_validator

from legal.models import AuthorityGrant
from proofgate.bindings.legal.resolver import LegalResolvedEffect
from proofgate.policy import RULE_WORKFLOW_BUDGET, PolicyDecision, RuleOutcome, RuleSet, evaluate_policy

RULE_MATTER_BOUNDARY = "RULE_MATTER_BOUNDARY"
RULE_PRIVILEGE_BOUNDARY = "RULE_PRIVILEGE_BOUNDARY"
RULE_RECIPIENT_BOUNDARY = "RULE_RECIPIENT_BOUNDARY"


class LegalPolicyContext(BaseModel):
    """The authority, the resolved effect, and the consequential document IDs
    already committed earlier in this workflow (supplied in memory)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    grant: AuthorityGrant
    effect: LegalResolvedEffect
    committed_resource_ids: tuple[str, ...] = ()

    @field_validator("committed_resource_ids")
    @classmethod
    def _normalize_committed(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))


def _details(authorized: object, resolved: object, reason: str | None, **evidence: object) -> MappingProxyType:
    return MappingProxyType({"authorized": authorized, "resolved": resolved, "reason": reason, **evidence})


class MatterBoundaryRule:
    rule_id = RULE_MATTER_BOUNDARY

    def evaluate(self, context: LegalPolicyContext, /) -> RuleOutcome:
        authorized = context.grant.matter_id
        resources = context.effect.consequential_resources
        matters = tuple(sorted({resource.matter_id for resource in resources}))
        unexpected = tuple(matter for matter in matters if matter != authorized)
        unexpected_ids = tuple(resource.doc_id for resource in resources if resource.matter_id != authorized)
        if unexpected:
            return RuleOutcome.failed(
                self.rule_id,
                f"Consequential documents span matters {list(unexpected)} outside authorized matter {authorized}.",
                f"consequential documents only in matter {authorized}",
                _details(
                    authorized,
                    matters,
                    "consequential_matter_outside_grant",
                    unexpected_matters=unexpected,
                    unexpected_resource_ids=unexpected_ids,
                ),
            )
        return RuleOutcome.passed(
            self.rule_id, _details(authorized, matters, None, unexpected_matters=(), unexpected_resource_ids=())
        )


class PrivilegeBoundaryRule:
    rule_id = RULE_PRIVILEGE_BOUNDARY

    def evaluate(self, context: LegalPolicyContext, /) -> RuleOutcome:
        policy = context.grant.privilege_policy
        privileged_ids = tuple(
            resource.doc_id for resource in context.effect.consequential_resources if resource.privileged
        )
        evidence = {"privileged_resource_ids": privileged_ids}
        if policy == "exclude" and privileged_ids:
            return RuleOutcome.failed(
                self.rule_id,
                f"{len(privileged_ids)} privileged documents are consequential but the grant excludes privileged content.",
                "no privileged consequential documents",
                _details(policy, len(privileged_ids), "privileged_content_excluded_by_grant", **evidence),
            )
        return RuleOutcome.passed(self.rule_id, _details(policy, len(privileged_ids), None, **evidence))


class WorkflowBudgetRule:
    """Legal budget unit: unique consequential documents across the
    workflow. Reads never count; overlap with committed IDs counts once."""

    rule_id = RULE_WORKFLOW_BUDGET

    def evaluate(self, context: LegalPolicyContext, /) -> RuleOutcome:
        maximum = context.grant.max_documents
        committed = set(context.committed_resource_ids)
        proposed = set(context.effect.consequential_resource_ids)
        projected = len(committed | proposed)
        evidence = {
            "committed_unique_count": len(committed),
            "proposed_unique_count": len(proposed),
            "overlap_resource_ids": tuple(sorted(committed & proposed)),
            "projected_unique_count": projected,
        }
        if projected > maximum:
            return RuleOutcome.failed(
                self.rule_id,
                f"{projected} unique consequential documents exceed the {maximum}-document workflow budget.",
                f"at most {maximum} unique consequential documents",
                _details(maximum, projected, "unique_consequential_documents_exceed_budget", **evidence),
            )
        return RuleOutcome.passed(self.rule_id, _details(maximum, projected, None, **evidence))


class RecipientBoundaryRule:
    rule_id = RULE_RECIPIENT_BOUNDARY

    def evaluate(self, context: LegalPolicyContext, /) -> RuleOutcome:
        allowed = context.grant.allowed_recipients
        resolved = context.effect.dimensions.external_recipients
        unauthorized = tuple(recipient for recipient in resolved if recipient not in allowed)
        evidence = {"unauthorized_recipients": unauthorized}
        if unauthorized:
            return RuleOutcome.failed(
                self.rule_id,
                f"Recipients {list(unauthorized)} are not authorized by the grant.",
                f"recipients only from {list(allowed)}",
                _details(allowed, resolved, "recipient_outside_grant", **evidence),
            )
        return RuleOutcome.passed(self.rule_id, _details(allowed, resolved, None, **evidence))


LEGAL_RULESET: RuleSet[LegalPolicyContext] = RuleSet(
    rules=(MatterBoundaryRule(), PrivilegeBoundaryRule(), WorkflowBudgetRule(), RecipientBoundaryRule()),
)


def evaluate_legal_rules(
    grant: AuthorityGrant,
    effect: LegalResolvedEffect,
    committed_resource_ids: tuple[str, ...] = (),
) -> PolicyDecision:
    """Evaluate the four rules for an already-valid grant and an
    already-resolved effect. Not an authorization entry point: grant
    validity and resolver failure are handled by authorize_legal_plan."""
    context = LegalPolicyContext(grant=grant, effect=effect, committed_resource_ids=committed_resource_ids)
    return evaluate_policy(context, LEGAL_RULESET)
