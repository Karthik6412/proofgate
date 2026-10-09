"""The proposed Legal plan: untrusted, ordered tool calls.

Deliberately not a workflow language. Three tools, fixed argument shapes,
and one reference form -- "$<step_id>.result" -- naming the result of an
earlier step. Shapes and references are checked by the resolver, which
fails closed on anything it does not recognize.

Plans are deeply immutable: arguments are stored as a name-sorted tuple of
(name, value) pairs whose values are immutable scalars, so nothing inside a
plan can be changed in place after construction.
"""

from typing import Annotated, Mapping

from pydantic import BaseModel, BeforeValidator, ConfigDict, StrictBool, StrictInt, StrictStr


def sorted_pairs(value: object) -> tuple[tuple[object, object], ...]:
    """Normalize a mapping (or pairs) to a name-sorted tuple of (name,
    value) pairs, rejecting duplicate names."""
    items = list(value.items()) if isinstance(value, Mapping) else [tuple(item) for item in value]
    names = [name for name, _ in items]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate names in {names}")
    return tuple(sorted(items, key=lambda item: item[0]))


Arguments = Annotated[
    tuple[tuple[StrictStr, StrictStr | StrictBool | StrictInt], ...],
    BeforeValidator(sorted_pairs),
]


class PlanStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: str
    tool: str
    arguments: Arguments

    def argument_map(self) -> dict[str, str | bool | int]:
        """A fresh dict copy for lookups; changing it never changes the step."""
        return dict(self.arguments)


class LegalPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: tuple[PlanStep, ...]


def _plan(include_related: bool) -> LegalPlan:
    return LegalPlan(
        steps=(
            PlanStep(
                step_id="step1",
                tool="search_documents",
                arguments={"matter": "M-102", "query": "diligence"},
            ),
            PlanStep(
                step_id="step2",
                tool="compile_package",
                arguments={"documents": "$step1.result", "include_related": include_related},
            ),
            PlanStep(
                step_id="step3",
                tool="share_package",
                arguments={"package": "$step2.result", "recipient": "counsel@example.com"},
            ),
        )
    )


# The Friday fixture plans. Canonical: related-document expansion reaches
# M-118. Clean: the same plan without expansion stays inside M-102.
CANONICAL_PLAN = _plan(include_related=True)
CLEAN_PLAN = _plan(include_related=False)
