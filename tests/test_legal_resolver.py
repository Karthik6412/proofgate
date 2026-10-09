"""Friday v1 Slice L2: deterministic, read-only Legal plan resolver.

The resolver resolves facts; it never sees authority and never decides.
These tests confirm it reproduces the L1 designed facts (80 search hits,
the 143-document one-hop package, the clean 80-document package), never
follows links transitively, keeps read resources apart from
consequential ones, leaks no document text, and fails closed.
"""

import ast
import builtins
import inspect
from collections import Counter
from pathlib import Path
from unittest import mock

import pytest
from pydantic import ValidationError

import proofgate.bindings.legal.resolver as resolver_module
from legal.corpus import build_corpus, corpus_digest, documents_by_id
from legal.models import Document
from proofgate.bindings.legal.plan import CANONICAL_PLAN, CLEAN_PLAN, LegalPlan, PlanStep
from proofgate.bindings.legal.resolver import ResolutionError, resolve_plan

SEARCH_HIT_IDS = tuple(f"D-102-{number:03d}" for number in range(1, 81))
M131_SECOND_HOP = {"D-131-001", "D-131-002", "D-131-003"}


def _step(step_id: str, tool: str, **arguments) -> PlanStep:
    return PlanStep(step_id=step_id, tool=tool, arguments=arguments)


def _plan(*steps: PlanStep) -> LegalPlan:
    return LegalPlan(steps=steps)


SEARCH = _step("step1", "search_documents", matter="M-102", query="diligence")


# ---------------------------------------------------------------------------
# A. Search
# ---------------------------------------------------------------------------


def test_canonical_search_resolves_exactly_the_80_m102_hits():
    search = resolve_plan(CANONICAL_PLAN).steps[0]

    assert search.tool == "search_documents"
    assert search.resolved_resource_ids == SEARCH_HIT_IDS
    assert dict(search.matters_touched) == {"M-102": 80}


def test_search_matching_is_case_insensitive_title_substring_within_matter():
    effect = resolve_plan(_plan(_step("s", "search_documents", matter="M-102", query="DILIGENCE workpaper")))
    assert effect.steps[0].resolved_resource_ids == SEARCH_HIT_IDS

    other_matter = resolve_plan(_plan(_step("s", "search_documents", matter="M-118", query="diligence")))
    assert other_matter.steps[0].resolved_resource_ids == ()


# ---------------------------------------------------------------------------
# B. One-hop expansion
# ---------------------------------------------------------------------------


def test_canonical_package_is_the_one_hop_expansion():
    effect = resolve_plan(CANONICAL_PLAN)
    compile_step = effect.steps[1]

    assert compile_step.input_resource_ids == SEARCH_HIT_IDS
    assert len(compile_step.resolved_resource_ids) == 143
    assert len(compile_step.added_by_expansion) == 63
    assert dict(compile_step.matters_touched) == {"M-102": 126, "M-118": 17}

    assert effect.total_affected == 143
    assert dict(effect.dimensions.matters_touched) == {"M-102": 126, "M-118": 17}
    assert effect.dimensions.privileged_count == 9
    assert effect.dimensions.restricted_count == 4


def test_expansion_reaches_m118_only_through_designed_links():
    effect = resolve_plan(CANONICAL_PLAN)
    m118_added = [doc_id for doc_id in effect.steps[1].added_by_expansion if doc_id.startswith("D-118-")]

    assert m118_added == [f"D-118-{number:03d}" for number in range(1, 18)]
    assert not any(doc_id.startswith("D-118-") for doc_id in effect.steps[0].resolved_resource_ids)


# ---------------------------------------------------------------------------
# C. No transitive expansion
# ---------------------------------------------------------------------------


def test_second_hop_m118_to_m131_links_are_not_followed():
    by_id = documents_by_id()
    effect = resolve_plan(CANONICAL_PLAN)
    package = set(effect.consequential_resource_ids)

    # The second-hop links really exist on package members...
    onward = {related for doc_id in package for related in by_id[doc_id].related_doc_ids} - package
    assert onward == M131_SECOND_HOP
    # ...and none of them is followed.
    assert not package & M131_SECOND_HOP
    assert "M-131" not in dict(effect.dimensions.matters_touched)
    assert not set(effect.all_resolved_resource_ids) & M131_SECOND_HOP


# ---------------------------------------------------------------------------
# D. Clean path
# ---------------------------------------------------------------------------


def test_clean_plan_keeps_the_80_document_m102_package():
    effect = resolve_plan(CLEAN_PLAN)

    assert effect.consequential_resource_ids == SEARCH_HIT_IDS
    assert effect.steps[1].added_by_expansion == ()
    assert dict(effect.dimensions.matters_touched) == {"M-102": 80}
    assert effect.dimensions.privileged_count == 0
    assert effect.dimensions.restricted_count == 0


# ---------------------------------------------------------------------------
# E. Consequential vs read resources
# ---------------------------------------------------------------------------


def test_documents_only_searched_are_resolved_but_not_consequential():
    effect = resolve_plan(
        _plan(
            SEARCH,
            _step("lookup", "search_documents", matter="M-118", query="agreement"),
            _step("step2", "compile_package", documents="$step1.result", include_related=False),
            _step("step3", "share_package", package="$step2.result", recipient="counsel@example.com"),
        )
    )
    looked_up = set(effect.steps[1].resolved_resource_ids)

    assert len(looked_up) == 17
    assert looked_up <= set(effect.all_resolved_resource_ids)
    assert not looked_up & set(effect.consequential_resource_ids)
    assert effect.consequential_resource_ids == SEARCH_HIT_IDS
    assert dict(effect.dimensions.matters_touched) == {"M-102": 80}
    assert effect.requested_scope.matters == ("M-102", "M-118")


def test_plan_without_a_share_step_has_no_consequential_resources():
    effect = resolve_plan(
        _plan(SEARCH, _step("step2", "compile_package", documents="$step1.result", include_related=True))
    )

    assert len(effect.all_resolved_resource_ids) == 143
    assert effect.consequential_resource_ids == ()
    assert effect.total_affected == 0
    assert effect.dimensions.external_recipients == ()


def test_consequential_resources_carry_only_stable_metadata():
    effect = resolve_plan(CANONICAL_PLAN)
    by_id = documents_by_id()

    assert [fact.doc_id for fact in effect.consequential_resources] == list(effect.consequential_resource_ids)
    for fact in effect.consequential_resources:
        document = by_id[fact.doc_id]
        assert (fact.matter_id, fact.privileged, fact.restricted, fact.content_hash) == (
            document.matter_id,
            document.privileged,
            document.restricted,
            document.content_hash,
        )


# ---------------------------------------------------------------------------
# F. Recipient
# ---------------------------------------------------------------------------


def test_share_resolves_the_exact_package_and_recipient():
    effect = resolve_plan(CANONICAL_PLAN)
    share = effect.steps[2]

    assert share.recipient == "counsel@example.com"
    assert share.input_resource_ids == share.resolved_resource_ids == effect.steps[1].resolved_resource_ids
    assert effect.dimensions.external_recipients == ("counsel@example.com",)
    assert effect.requested_scope.recipients == ("counsel@example.com",)


# ---------------------------------------------------------------------------
# G. Determinism
# ---------------------------------------------------------------------------


def test_resolution_is_deterministic_across_runs():
    first = resolve_plan(CANONICAL_PLAN)
    for _ in range(5):
        again = resolve_plan(CANONICAL_PLAN)
        assert again == first
        assert again.model_dump_json() == first.model_dump_json()


def test_resolution_does_not_depend_on_source_iteration_order():
    by_id = documents_by_id()
    reversed_source = dict(reversed(list(by_id.items())))

    assert resolve_plan(CANONICAL_PLAN, documents=reversed_source) == resolve_plan(CANONICAL_PLAN)


# ---------------------------------------------------------------------------
# G2. Deep immutability
# ---------------------------------------------------------------------------


def _assert_pairs_cannot_be_mutated(pairs):
    assert isinstance(pairs, tuple) and all(isinstance(pair, tuple) for pair in pairs)
    with pytest.raises(TypeError):
        pairs[0] = ("injected", "value")
    with pytest.raises(TypeError):
        pairs[0][1] = "injected"
    with pytest.raises(AttributeError):
        pairs.append(("injected", "value"))
    with pytest.raises(TypeError):
        del pairs[0]


def test_plan_step_arguments_cannot_be_mutated():
    step = CANONICAL_PLAN.steps[1]

    _assert_pairs_cannot_be_mutated(step.arguments)
    with pytest.raises(ValidationError):
        step.arguments = (("documents", "$step1.result"), ("include_related", False))

    lookup = step.argument_map()
    lookup["include_related"] = False
    assert dict(step.arguments)["include_related"] is True
    assert resolve_plan(CANONICAL_PLAN).total_affected == 143


def test_trace_arguments_cannot_be_mutated():
    _assert_pairs_cannot_be_mutated(resolve_plan(CANONICAL_PLAN).steps[1].arguments)


def test_trace_matter_counts_cannot_be_mutated():
    _assert_pairs_cannot_be_mutated(resolve_plan(CANONICAL_PLAN).steps[1].matters_touched)


def test_effect_dimension_matter_counts_cannot_be_mutated():
    _assert_pairs_cannot_be_mutated(resolve_plan(CANONICAL_PLAN).dimensions.matters_touched)


def test_keyed_writes_into_plans_and_effects_are_impossible():
    """The literal attacks: writing a key into a plan or effect field."""
    step = CANONICAL_PLAN.steps[0]
    effect = resolve_plan(CANONICAL_PLAN)

    with pytest.raises(TypeError):
        step.arguments["matter"] = "M-118"
    with pytest.raises(TypeError):
        effect.steps[0].arguments["matter"] = "M-118"
    with pytest.raises(TypeError):
        effect.steps[1].matters_touched["M-118"] = 0
    with pytest.raises(TypeError):
        effect.dimensions.matters_touched["M-118"] = 999

    assert dict(step.arguments)["matter"] == "M-102"
    assert dict(effect.dimensions.matters_touched) == {"M-102": 126, "M-118": 17}


def test_plans_and_effects_are_hashable_all_the_way_down():
    """hash() fails on any nested mutable container (dict, list, set)."""
    assert hash(CANONICAL_PLAN) == hash(CANONICAL_PLAN.model_copy())
    assert hash(resolve_plan(CANONICAL_PLAN)) == hash(resolve_plan(CANONICAL_PLAN))


def test_argument_order_is_normalized_and_duplicates_rejected():
    forward = PlanStep(step_id="s", tool="search_documents", arguments={"matter": "M-102", "query": "diligence"})
    backward = PlanStep(step_id="s", tool="search_documents", arguments={"query": "diligence", "matter": "M-102"})

    assert forward == backward
    assert forward.arguments == (("matter", "M-102"), ("query", "diligence"))
    with pytest.raises(ValidationError):
        PlanStep(step_id="s", tool="search_documents", arguments=(("matter", "M-102"), ("matter", "M-118")))


@pytest.mark.parametrize("value", [["M-102"], {"m": "M-102"}, 1.5, None], ids=["list", "dict", "float", "none"])
def test_argument_values_must_be_immutable_scalars(value):
    with pytest.raises(ValidationError):
        PlanStep(step_id="s", tool="search_documents", arguments={"matter": value, "query": "diligence"})


def test_argument_values_keep_their_exact_types():
    arguments = dict(
        PlanStep(step_id="s", tool="t", arguments={"flag": True, "word": "yes", "number": 1}).arguments
    )

    assert (type(arguments["flag"]), type(arguments["word"]), type(arguments["number"])) == (bool, str, int)


# ---------------------------------------------------------------------------
# H. Read-only
# ---------------------------------------------------------------------------


def test_resolution_leaves_the_corpus_unchanged():
    before_digest = corpus_digest()
    before_documents = build_corpus()

    resolve_plan(CANONICAL_PLAN)
    resolve_plan(CLEAN_PLAN)

    assert corpus_digest() == before_digest
    assert build_corpus() is before_documents


def test_resolution_opens_no_files():
    documents_by_id()  # corpus is built in memory; warm the cache first
    with mock.patch.object(builtins, "open", side_effect=AssertionError("resolver opened a file")):
        resolve_plan(CANONICAL_PLAN)


# ---------------------------------------------------------------------------
# I. No text leakage
# ---------------------------------------------------------------------------


def test_effect_contains_no_document_text_or_canary():
    serialized = resolve_plan(CANONICAL_PLAN).model_dump_json()

    assert "Synthetic text" not in serialized
    assert "BLUE ORCHID" not in serialized
    assert "Acquisition codename" not in serialized
    for document in build_corpus():
        assert document.text not in serialized


def test_effect_models_have_no_text_fields():
    for model in (
        resolver_module.LegalResolvedEffect,
        resolver_module.StepTrace,
        resolver_module.ResourceFacts,
        resolver_module.LegalEffectDimensions,
        resolver_module.RequestedScope,
    ):
        assert not {"text", "canary", "title"} & set(model.model_fields), model.__name__


# ---------------------------------------------------------------------------
# J. Independence from authority
# ---------------------------------------------------------------------------


def test_resolver_signature_takes_no_authority():
    assert list(inspect.signature(resolve_plan).parameters) == ["plan", "documents"]


def test_resolver_imports_no_authority_policy_or_registry():
    tree = ast.parse(Path(resolver_module.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)

    forbidden = ("legal.grants", "legal.permissions", "proofgate.policy", "proofgate.registry", "proofgate.core")
    assert not [module for module in imported if module.startswith(forbidden)]
    assert "legal.models.AuthorityGrant" not in imported


def test_resolver_code_has_no_verdict_or_authority_concepts():
    tree = ast.parse(Path(resolver_module.__file__).read_text())
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    forbidden = {"AuthorityGrant", "grant", "evaluate_policy", "RuleSet", "ALLOW", "BLOCK", "verdict"}
    found = set()
    for node in ast.walk(tree):
        if id(node) in docstrings:
            continue
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.update(token for token in forbidden if token in node.value)
    assert not found & forbidden


# ---------------------------------------------------------------------------
# K. Fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "plan,message",
    [
        (_plan(), "no steps"),
        (_plan(_step("s", "send_email", to="x@example.com")), "unknown tool"),
        (_plan(SEARCH, _step("c", "compile_package", documents="$nope.result", include_related=True)), "unknown or later"),
        (
            _plan(_step("c", "compile_package", documents="$s.result", include_related=True), SEARCH),
            "unknown or later",
        ),
        (_plan(SEARCH, _step("c", "compile_package", documents="step1", include_related=True)), r"not a \$<step>\.result"),
        (_plan(SEARCH, _step("c", "compile_package", documents="$step1.output", include_related=True)), r"not a \$<step>\.result"),
        (_plan(SEARCH, _step("x", "share_package", package="$step1.result", recipient="counsel@example.com")), "needs a package"),
        (_plan(SEARCH, _step("c", "compile_package", documents="$step1.result", include_related="yes")), "non-empty bool"),
        (_plan(_step("s", "search_documents", matter="M-102")), "takes exactly"),
        (_plan(_step("s", "search_documents", matter="M-102", query="diligence", limit=5)), "takes exactly"),
        (_plan(_step("s", "search_documents", matter="", query="diligence")), "non-empty str"),
        (_plan(SEARCH, SEARCH), "Duplicate step id"),
    ],
    ids=[
        "empty_plan",
        "unknown_tool",
        "dangling_reference",
        "forward_reference",
        "bare_reference",
        "malformed_reference",
        "wrong_result_kind",
        "non_bool_flag",
        "missing_argument",
        "extra_argument",
        "empty_matter",
        "duplicate_step",
    ],
)
def test_unresolvable_plans_fail_closed(plan, message):
    with pytest.raises(ResolutionError, match=message):
        resolve_plan(plan)


def test_link_to_a_missing_document_fails_closed():
    source = {
        "A-1": Document(
            doc_id="A-1", matter_id="M-102", client_id="C-01", title="Diligence A", status="final",
            document_type="memo", privileged=False, restricted=False, canary=None, text="x",
            related_doc_ids=("A-404",),
        )
    }
    plan = _plan(
        _step("s", "search_documents", matter="M-102", query="diligence"),
        _step("c", "compile_package", documents="$s.result", include_related=True),
    )

    with pytest.raises(ResolutionError, match="unknown documents"):
        resolve_plan(plan, documents=source)


def test_counts_match_the_l1_corpus_independently():
    """Cross-check the resolver against a direct count over corpus fields."""
    by_id = documents_by_id()
    effect = resolve_plan(CANONICAL_PLAN)

    assert Counter(by_id[doc_id].matter_id for doc_id in effect.consequential_resource_ids) == {
        "M-102": 126,
        "M-118": 17,
    }
