"""Deterministic, read-only resolution of a proposed Legal plan.

The resolver resolves facts; policy decides whether they are authorized.
It takes only the plan and the document source -- never an
AuthorityGrant -- and it never filters what it resolves to fit any scope:
if expansion reaches another matter, the effect says so.

Resolution rules
    search_documents  executes read-only: documents whose matter_id equals
                      `matter` and whose title contains `query`,
                      case-insensitively, ordered by doc_id.
    compile_package   resolves membership: the input documents, plus --
                      when include_related is true -- exactly one hop of
                      each input document's related_doc_ids. Never
                      transitive.
    share_package     resolves the exact package members and recipient.
                      Nothing is sent or written.

The effect carries metadata only; document text never leaves this module.
Like the plan, it is deeply immutable: per-matter counts are name-sorted
tuples of (matter_id, count) pairs, never mutable dicts.
"""

import re
from collections import Counter
from typing import Annotated, Mapping

from pydantic import BaseModel, BeforeValidator, ConfigDict, StrictInt, StrictStr

from legal.corpus import documents_by_id
from legal.models import Document
from proofgate.bindings.legal.plan import Arguments, LegalPlan, PlanStep, sorted_pairs

_REFERENCE = re.compile(r"^\$(?P<step_id>[A-Za-z0-9_]+)\.result$")

# The kind of result each tool produces, and the kind each reference
# argument accepts.
_RESULT_KIND = {"search_documents": "documents", "compile_package": "package", "share_package": "share"}
_ARGUMENTS = {
    "search_documents": {"matter": str, "query": str},
    "compile_package": {"documents": str, "include_related": bool},
    "share_package": {"package": str, "recipient": str},
}
_REFERENCE_ARGUMENT_KIND = {("compile_package", "documents"): "documents", ("share_package", "package"): "package"}


MatterCounts = Annotated[tuple[tuple[StrictStr, StrictInt], ...], BeforeValidator(sorted_pairs)]


class ResolutionError(Exception):
    """The plan cannot be resolved to a known effect."""


class RequestedScope(BaseModel):
    """What the plan's own arguments name, before resolution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    matters: tuple[str, ...]
    recipients: tuple[str, ...]


class StepTrace(BaseModel):
    """What one step asked for and what it resolved to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: str
    tool: str
    arguments: Arguments
    input_resource_ids: tuple[str, ...]
    resolved_resource_ids: tuple[str, ...]
    added_by_expansion: tuple[str, ...]
    matters_touched: MatterCounts
    recipient: str | None


class ResourceFacts(BaseModel):
    """Stable per-resource metadata for a consequential resource. No text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    doc_id: str
    matter_id: str
    privileged: bool
    restricted: bool
    content_hash: str


class LegalEffectDimensions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    matters_touched: MatterCounts
    privileged_count: int
    restricted_count: int
    external_recipients: tuple[str, ...]


class LegalResolvedEffect(BaseModel):
    """The resolved effect of a plan.

    all_resolved_resource_ids: every document any step resolved, including
    ones only searched or inspected. consequential_resource_ids: the
    documents a share step would act on; only these feed the effect's
    dimensions and total_affected.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_scope: RequestedScope
    steps: tuple[StepTrace, ...]
    all_resolved_resource_ids: tuple[str, ...]
    consequential_resource_ids: tuple[str, ...]
    consequential_resources: tuple[ResourceFacts, ...]
    total_affected: int
    dimensions: LegalEffectDimensions


def _check_arguments(step: PlanStep, arguments: dict) -> None:
    expected = _ARGUMENTS[step.tool]
    if set(arguments) != set(expected):
        raise ResolutionError(
            f"Step {step.step_id!r}: {step.tool} takes exactly {sorted(expected)}, got {sorted(arguments)}."
        )
    for name, kind in expected.items():
        value = arguments[name]
        if type(value) is not kind or (kind is str and not value):
            raise ResolutionError(f"Step {step.step_id!r}: argument {name!r} must be a non-empty {kind.__name__}.")


def _dereference(
    step: PlanStep, arguments: dict, name: str, results: dict[str, tuple[str, tuple[str, ...]]]
) -> tuple[str, ...]:
    match = _REFERENCE.match(arguments[name])
    if match is None:
        raise ResolutionError(f"Step {step.step_id!r}: {name!r} is not a $<step>.result reference.")
    target = match.group("step_id")
    if target not in results:
        raise ResolutionError(f"Step {step.step_id!r}: {name!r} references unknown or later step {target!r}.")
    kind, ids = results[target]
    expected_kind = _REFERENCE_ARGUMENT_KIND[(step.tool, name)]
    if kind != expected_kind:
        raise ResolutionError(f"Step {step.step_id!r}: {name!r} needs a {expected_kind} result; {target!r} is {kind}.")
    return ids


def _matters(ids: tuple[str, ...], documents: Mapping[str, Document]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(Counter(documents[doc_id].matter_id for doc_id in ids).items()))


def resolve_plan(plan: LegalPlan, documents: Mapping[str, Document] | None = None) -> LegalResolvedEffect:
    """Resolve every step in order. Raises ResolutionError rather than
    skipping anything it cannot resolve."""
    source = documents_by_id() if documents is None else documents
    if not plan.steps:
        raise ResolutionError("Plan has no steps.")

    results: dict[str, tuple[str, tuple[str, ...]]] = {}
    traces: list[StepTrace] = []
    shared: list[tuple[tuple[str, ...], str]] = []

    for step in plan.steps:
        if step.tool not in _ARGUMENTS:
            raise ResolutionError(f"Step {step.step_id!r}: unknown tool {step.tool!r}.")
        if step.step_id in results:
            raise ResolutionError(f"Duplicate step id {step.step_id!r}.")
        arguments = step.argument_map()
        _check_arguments(step, arguments)

        input_ids: tuple[str, ...] = ()
        added: tuple[str, ...] = ()
        recipient = None
        if step.tool == "search_documents":
            matter, query = arguments["matter"], arguments["query"].lower()
            resolved = tuple(
                sorted(
                    doc_id
                    for doc_id, document in source.items()
                    if document.matter_id == matter and query in document.title.lower()
                )
            )
        elif step.tool == "compile_package":
            input_ids = _dereference(step, arguments, "documents", results)
            members = set(input_ids)
            if arguments["include_related"]:
                for doc_id in input_ids:
                    members.update(source[doc_id].related_doc_ids)
            unknown = sorted(members - set(source))
            if unknown:
                raise ResolutionError(f"Step {step.step_id!r}: unknown documents {unknown}.")
            resolved = tuple(sorted(members))
            added = tuple(sorted(members - set(input_ids)))
        else:
            input_ids = _dereference(step, arguments, "package", results)
            resolved = input_ids
            recipient = arguments["recipient"]
            shared.append((resolved, recipient))

        results[step.step_id] = (_RESULT_KIND[step.tool], resolved)
        traces.append(
            StepTrace(
                step_id=step.step_id,
                tool=step.tool,
                arguments=step.arguments,
                input_resource_ids=input_ids,
                resolved_resource_ids=resolved,
                added_by_expansion=added,
                matters_touched=_matters(resolved, source),
                recipient=recipient,
            )
        )

    all_resolved = tuple(sorted({doc_id for _, ids in results.values() for doc_id in ids}))
    consequential = tuple(sorted({doc_id for ids, _ in shared for doc_id in ids}))
    recipients = tuple(sorted({recipient for _, recipient in shared}))
    facts = tuple(
        ResourceFacts(
            doc_id=doc_id,
            matter_id=source[doc_id].matter_id,
            privileged=source[doc_id].privileged,
            restricted=source[doc_id].restricted,
            content_hash=source[doc_id].content_hash,
        )
        for doc_id in consequential
    )
    return LegalResolvedEffect(
        requested_scope=RequestedScope(
            matters=tuple(sorted({s.argument_map()["matter"] for s in plan.steps if s.tool == "search_documents"})),
            recipients=recipients,
        ),
        steps=tuple(traces),
        all_resolved_resource_ids=all_resolved,
        consequential_resource_ids=consequential,
        consequential_resources=facts,
        total_affected=len(consequential),
        dimensions=LegalEffectDimensions(
            matters_touched=_matters(consequential, source),
            privileged_count=sum(fact.privileged for fact in facts),
            restricted_count=sum(fact.restricted for fact in facts),
            external_recipients=recipients,
        ),
    )
