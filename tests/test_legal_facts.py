"""Friday v1 Slice L1: deterministic synthetic Legal facts.

Facts only -- no resolver, no policy, no ALLOW/BLOCK. The structural
facts pinned here are the ones later slices depend on: the canonical
M-102 search hits, the one-hop expansion into M-118, and the clean
M-102-only subset for the future ALLOW path. Search and expansion are
computed here directly from corpus fields using the matching rule
documented in legal/corpus.py; the resolver itself is L2.
"""

import ast
import datetime
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

import legal
from legal.corpus import (
    CANONICAL_MATTER,
    CANONICAL_QUERY,
    EXPANSION_MATTER,
    OTHER_MATTER,
    build_corpus,
    corpus_digest,
    documents_by_id,
)
from legal.grants import GrantValidity, grant_validity
from legal.models import AuthorityGrant, Document, PermissionSnapshot
from legal.permissions import SEEDED_USER_ID, get_permission_snapshot

PINNED_CORPUS_DIGEST = "5fad8962ffb628983515c06f0f38dc79687f27094aa1766f5e93d7542ce88db7"
FUTURE_MAX_DOCUMENTS = 100  # the canonical grant's document budget

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 9, 9, 0, tzinfo=UTC)


def _search_hits() -> list[Document]:
    """The intended L2 search rule: matter filter plus case-insensitive
    title substring."""
    return [
        document
        for document in build_corpus()
        if document.matter_id == CANONICAL_MATTER and CANONICAL_QUERY in document.title.lower()
    ]


def _one_hop_package(hits: list[Document]) -> list[Document]:
    by_id = documents_by_id()
    ids = {document.doc_id for document in hits} | {
        related for document in hits for related in document.related_doc_ids
    }
    return [by_id[doc_id] for doc_id in sorted(ids)]


def _grant(**overrides) -> AuthorityGrant:
    fields = dict(
        grant_id="grant_001",
        workflow_id="wf_123",
        matter_id="M-102",
        max_documents=100,
        privilege_policy="exclude",
        allowed_recipients=("counsel@example.com",),
        granted_by="partner-01",
        granted_at=T0,
        expires_at=T0 + datetime.timedelta(hours=8),
    )
    fields.update(overrides)
    return AuthorityGrant(**fields)


def _snapshot(**overrides) -> PermissionSnapshot:
    fields = dict(
        user_id="associate-01",
        permitted_matters=frozenset({"M-102", "M-118"}),
        restricted_document_ids=frozenset({"D-118-013", "D-102-119"}),
        permitted_recipients=frozenset({"counsel@example.com"}),
        source_version="seed-v1",
        captured_at=T0,
    )
    fields.update(overrides)
    return PermissionSnapshot(**fields)


# ---------------------------------------------------------------------------
# A. Determinism
# ---------------------------------------------------------------------------


def test_corpus_digest_is_pinned():
    assert corpus_digest() == PINNED_CORPUS_DIGEST


def test_corpus_is_identical_across_independent_processes():
    """A fresh interpreter (fresh hash seed) rebuilds the same corpus."""
    output = subprocess.run(
        [sys.executable, "-c", "from legal.corpus import corpus_digest; print(corpus_digest())"],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(legal.__file__).resolve().parent.parent,
    ).stdout.strip()
    assert output == PINNED_CORPUS_DIGEST


def test_repeated_construction_yields_identical_ordered_facts():
    build_corpus.cache_clear()
    first = build_corpus()
    build_corpus.cache_clear()
    second = build_corpus()

    assert first == second
    assert [document.doc_id for document in first] == sorted(document.doc_id for document in first)


# ---------------------------------------------------------------------------
# B. Corpus structure
# ---------------------------------------------------------------------------


def test_corpus_structural_facts():
    corpus = build_corpus()

    assert len(corpus) == 186
    assert Counter(document.matter_id for document in corpus) == {"M-102": 136, "M-118": 30, "M-131": 20}
    assert {document.matter_id: document.client_id for document in corpus} == {
        "M-102": "C-01",
        "M-118": "C-01",
        "M-131": "C-02",
    }
    assert sum(document.privileged for document in corpus) == 16
    assert sum(document.restricted for document in corpus) == 7


def test_every_restricted_document_and_only_those_carry_a_unique_canary():
    corpus = build_corpus()
    canaries = [document.canary for document in corpus if document.canary is not None]

    assert all((document.canary is not None) == document.restricted for document in corpus)
    assert len(canaries) == len(set(canaries)) == 7
    assert all(document.canary in document.text for document in corpus if document.canary)


def test_document_ids_are_unique_and_links_resolve():
    corpus = build_corpus()
    ids = [document.doc_id for document in corpus]

    assert len(ids) == len(set(ids))
    assert all(related in set(ids) for document in corpus for related in document.related_doc_ids)


def test_content_hash_is_derived_from_text():
    by_id = documents_by_id()
    document = by_id["D-102-001"]

    assert document.content_hash == "071ba8892304e7eb69d0466b4fc2786284d50359897e17c02579817b3a2fe46e"
    edited = document.model_copy(update={"text": document.text + " edited"})
    assert edited.content_hash != document.content_hash


def test_document_is_immutable_and_links_are_normalized():
    document = documents_by_id()["D-102-001"]
    with pytest.raises(ValidationError):
        document.matter_id = "M-118"

    linked = Document(
        doc_id="X-1", matter_id="M-102", client_id="C-01", title="t", status="final",
        document_type="memo", privileged=False, restricted=False, canary=None, text="x",
        related_doc_ids=("X-3", "X-2", "X-3"),
    )
    assert linked.related_doc_ids == ("X-2", "X-3")


# ---------------------------------------------------------------------------
# C. Canonical M-102 search fact
# ---------------------------------------------------------------------------


def test_canonical_search_hits_are_80_m102_documents_only():
    hits = _search_hits()

    assert len(hits) == 80
    assert {document.matter_id for document in hits} == {CANONICAL_MATTER}


def test_no_document_outside_m102_matches_the_query_even_without_a_matter_filter():
    """M-118 can enter the canonical package only through link expansion."""
    matching = [document for document in build_corpus() if CANONICAL_QUERY in document.title.lower()]

    assert {document.matter_id for document in matching} == {CANONICAL_MATTER}


# ---------------------------------------------------------------------------
# D. Expansion path
# ---------------------------------------------------------------------------


def test_designed_search_hits_link_one_hop_into_m118():
    by_id = documents_by_id()
    sources = {
        document.doc_id: [by_id[related].matter_id for related in document.related_doc_ids]
        for document in _search_hits()
        if any(by_id[related].matter_id == EXPANSION_MATTER for related in document.related_doc_ids)
    }

    assert sorted(sources) == [f"D-102-{number:03d}" for number in range(47, 64)]
    assert all(matters == [EXPANSION_MATTER] for matters in sources.values())


def test_one_hop_package_matches_the_canonical_numbers():
    package = _one_hop_package(_search_hits())

    assert len(package) == 143
    assert Counter(document.matter_id for document in package) == {"M-102": 126, "M-118": 17}
    assert sum(document.privileged for document in package) == 9
    assert sum(document.restricted for document in package) == 4
    assert len(package) > FUTURE_MAX_DOCUMENTS


def test_m131_is_reachable_only_by_a_second_hop():
    package_ids = {document.doc_id for document in _one_hop_package(_search_hits())}
    by_id = documents_by_id()
    second_hop = {
        related for doc_id in package_ids for related in by_id[doc_id].related_doc_ids
    } - package_ids

    assert all(by_id[doc_id].matter_id != OTHER_MATTER for doc_id in package_ids)
    assert second_hop == {"D-131-001", "D-131-002", "D-131-003"}


# ---------------------------------------------------------------------------
# E. Clean ALLOW path
# ---------------------------------------------------------------------------


def test_clean_m102_subset_exists_within_budget():
    """The search hits alone (no related expansion) are the future ALLOW
    package: M-102 only, nothing privileged or restricted, within budget."""
    hits = _search_hits()

    assert len(hits) <= FUTURE_MAX_DOCUMENTS
    assert {document.matter_id for document in hits} == {CANONICAL_MATTER}
    assert not any(document.privileged for document in hits)
    assert not any(document.restricted for document in hits)


# ---------------------------------------------------------------------------
# F. Permission snapshot digest
# ---------------------------------------------------------------------------


def test_snapshot_digest_ignores_set_order_and_captured_at():
    first = _snapshot()
    reordered = _snapshot(
        permitted_matters=frozenset(["M-118", "M-102"]),
        restricted_document_ids=frozenset(["D-102-119", "D-118-013"]),
        captured_at=T0 + datetime.timedelta(minutes=5),
    )

    assert first.snapshot_digest == reordered.snapshot_digest


@pytest.mark.parametrize(
    "override",
    [
        {"user_id": "associate-02"},
        {"permitted_matters": frozenset({"M-102"})},
        {"restricted_document_ids": frozenset({"D-118-013"})},
        {"permitted_recipients": frozenset({"counsel@example.com", "other@example.com"})},
        {"source_version": "seed-v2"},
    ],
    ids=["user", "matters", "restricted", "recipients", "source_version"],
)
def test_snapshot_digest_changes_with_any_permission_field(override):
    assert _snapshot(**override).snapshot_digest != _snapshot().snapshot_digest


def test_snapshot_digest_is_the_shared_digest_of_sorted_canonical_state():
    from proofgate.selector import sha256_digest

    snapshot = _snapshot()
    assert snapshot.canonical_state() == {
        "user_id": "associate-01",
        "permitted_matters": ["M-102", "M-118"],
        "restricted_document_ids": ["D-102-119", "D-118-013"],
        "permitted_recipients": ["counsel@example.com"],
        "source_version": "seed-v1",
    }
    assert snapshot.snapshot_digest == sha256_digest(snapshot.canonical_state())


def test_seeded_provider_returns_the_seeded_snapshot():
    snapshot = get_permission_snapshot(SEEDED_USER_ID, captured_at=T0)
    later = get_permission_snapshot(SEEDED_USER_ID, captured_at=T0 + datetime.timedelta(days=1))

    assert snapshot.permitted_matters == frozenset({"M-102", "M-118"})
    assert snapshot.restricted_document_ids == frozenset(
        document.doc_id for document in build_corpus() if document.restricted
    )
    assert snapshot.permitted_recipients == frozenset({"counsel@example.com"})
    assert snapshot.snapshot_digest == later.snapshot_digest
    assert get_permission_snapshot("someone-else", captured_at=T0) is None


# ---------------------------------------------------------------------------
# G. Grant
# ---------------------------------------------------------------------------


def test_grant_is_immutable():
    grant = _grant()
    with pytest.raises(ValidationError):
        grant.max_documents = 1000
    with pytest.raises(AttributeError):
        grant.allowed_recipients.append("attacker@example.com")


def test_grant_recipients_are_normalized_without_widening():
    grant = _grant(allowed_recipients=("b@example.com", "a@example.com", "b@example.com"))

    assert grant.allowed_recipients == ("a@example.com", "b@example.com")
    assert _grant(allowed_recipients=("Counsel@Example.com",)).allowed_recipients == ("Counsel@Example.com",)


@pytest.mark.parametrize(
    "override",
    [
        {"max_documents": 0},
        {"matter_id": ""},
        {"privilege_policy": "sometimes"},
        {"granted_at": datetime.datetime(2026, 10, 9, 9, 0)},
        {"expires_at": T0},
        {"unexpected_field": "x"},
    ],
    ids=["zero_budget", "empty_matter", "bad_policy", "naive_time", "expires_at_issue", "extra_field"],
)
def test_invalid_grants_are_rejected(override):
    with pytest.raises(ValidationError):
        _grant(**override)


def test_grant_validity():
    grant = _grant()

    assert grant_validity(grant, now=T0) == GrantValidity.VALID
    assert grant_validity(grant, now=T0 + datetime.timedelta(hours=7, minutes=59)) == GrantValidity.VALID
    assert grant_validity(grant, now=grant.expires_at) == GrantValidity.EXPIRED
    assert grant_validity(None, now=T0) == GrantValidity.MISSING
    with pytest.raises(ValueError):
        grant_validity(grant, now=datetime.datetime(2026, 10, 9, 9, 0))


# ---------------------------------------------------------------------------
# H. Separation
# ---------------------------------------------------------------------------

LEGAL_DIR = Path(legal.__file__).resolve().parent
PROOFGATE_DIR = LEGAL_DIR.parent / "proofgate"


def _imported_modules(path: Path) -> set[str]:
    modules = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_legal_facts_import_only_the_shared_digest_from_proofgate():
    for path in sorted(LEGAL_DIR.glob("*.py")):
        proofgate_imports = {module for module in _imported_modules(path) if module.split(".")[0] == "proofgate"}
        assert proofgate_imports <= {"proofgate.selector"}, path.name
        assert not {module for module in _imported_modules(path) if module.split(".")[0] in {"operations", "agent", "craft"}}


def test_legal_facts_contain_no_decision_or_resolver_concepts():
    """Identifiers and code string literals only; docstrings may (and do)
    state what this package never does."""
    forbidden = {"evaluate_policy", "RuleSet", "PolicyDecision", "guarded_execute", "register_tool", "ALLOW", "BLOCK"}
    for path in sorted(LEGAL_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        names = set()
        for node in ast.walk(tree):
            if id(node) in docstrings:
                continue
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.update(token for token in forbidden if token in node.value)
        assert not names & forbidden, path.name


REPO_ROOT = LEGAL_DIR.parent
LEGAL_BINDING_DIR = PROOFGATE_DIR / "bindings" / "legal"


def _module_name(path: Path) -> str:
    parts = path.relative_to(REPO_ROOT).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _absolute_imports(source: str, module: str, is_package: bool) -> set[str]:
    """Every module a source file imports, with relative imports resolved
    and `from package import name` also recorded as `package.name`."""
    package = module if is_package else module.rpartition(".")[0]
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            imported.add(base)
            imported.update(f"{base}.{alias.name}" for alias in node.names)
    return imported


def _legal_imports(source: str, module: str, is_package: bool = False) -> set[str]:
    return {
        name
        for name in _absolute_imports(source, module, is_package)
        if name == "legal"
        or name.startswith("legal.")
        or name == "proofgate.bindings.legal"
        or name.startswith("proofgate.bindings.legal.")
    }


def test_only_the_legal_binding_imports_the_legal_domain():
    """Only proofgate/bindings/legal/** may import legal or the Legal
    binding. Everything else under proofgate/ -- generic code and the
    Operations binding alike -- stays independent of Legal."""
    checked = []
    for path in sorted(PROOFGATE_DIR.rglob("*.py")):
        if LEGAL_BINDING_DIR == path.parent or LEGAL_BINDING_DIR in path.parents:
            continue
        checked.append(path)
        found = _legal_imports(path.read_text(), _module_name(path), path.name == "__init__.py")
        assert not found, f"{path.relative_to(REPO_ROOT)} imports {sorted(found)}"

    checked_names = {path.relative_to(PROOFGATE_DIR).as_posix() for path in checked}
    assert {"bindings/operations.py", "bindings/__init__.py", "core.py", "policy.py", "registry.py"} <= checked_names


@pytest.mark.parametrize(
    "source",
    [
        "import legal",
        "import legal.models",
        "from legal.corpus import build_corpus",
        "from legal import corpus",
        "import proofgate.bindings.legal.resolver",
        "from proofgate.bindings.legal.resolver import resolve_plan",
        "from proofgate.bindings import legal",
        "from .legal import resolver",
        "from .legal.resolver import resolve_plan",
        "from ..bindings.legal import plan",
    ],
)
def test_boundary_check_catches_legal_imports_in_the_operations_binding(source):
    """Positive control: each of these would be caught in
    proofgate/bindings/operations.py."""
    assert _legal_imports(source, "proofgate.bindings.operations")


@pytest.mark.parametrize(
    "source",
    ["import operations.actions", "from proofgate.registry import register_tool", "import legalese", "from . import operations"],
)
def test_boundary_check_ignores_non_legal_imports(source):
    assert not _legal_imports(source, "proofgate.bindings.operations")


def test_a_legal_import_added_to_the_real_operations_binding_is_caught():
    path = PROOFGATE_DIR / "bindings" / "operations.py"
    source = path.read_text()

    assert not _legal_imports(source, _module_name(path))
    assert _legal_imports(source + "\nfrom legal.models import Document\n", _module_name(path))
