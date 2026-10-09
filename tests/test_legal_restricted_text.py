"""Friday v1 definition of done: normal flow never exposes restricted text.

Raw document text (and canary markers) must stop at the source Document:
the resolver may read metadata -- IDs, matters, labels, titles for search,
and a one-way content hash -- but no artifact past it may carry text. This
plants distinctive sentinel text in two source documents, runs the real
authorization, binding and decision-record flow, and checks every
serialized artifact plus the persisted JSONL. Structural guards keep raw
.text / .canary access confined to Document.content_hash and keep model
clients out of the Legal Action Boundary.
"""

import ast
import datetime
from pathlib import Path

import pytest

import legal
import proofgate.bindings.legal as legal_binding
from legal.corpus import build_corpus, documents_by_id
from legal.models import AuthorityGrant
from legal.permissions import SEEDED_AGENT_ID, SEEDED_USER_ID, get_agent_capabilities, get_permission_snapshot
from proofgate.bindings.legal.authorization import LegalAction
from proofgate.bindings.legal.decision_record import authorize_and_record
from proofgate.bindings.legal.plan import CANONICAL_PLAN, CLEAN_PLAN
from proofgate.bindings.legal.rules import LegalPolicyContext
from proofgate.selector import canonical_json, sha256_digest

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
NOW = T0 + datetime.timedelta(hours=1)
PLAIN_SENTINEL = "SENTINEL-PLAIN-7f3a9c Confidential deal terms"
RESTRICTED_SENTINEL = "SENTINEL-RESTRICTED-2b81d4 Ethical-wall material"
PLAIN_DOC = "D-102-001"  # in the clean (ALLOW) package
RESTRICTED_DOC = "D-102-119"  # restricted, in the canonical (BLOCK) package


def _source():
    source = dict(documents_by_id())
    source[PLAIN_DOC] = source[PLAIN_DOC].model_copy(update={"text": PLAIN_SENTINEL})
    source[RESTRICTED_DOC] = source[RESTRICTED_DOC].model_copy(
        update={"text": f"{RESTRICTED_SENTINEL} Acquisition codename: {source[RESTRICTED_DOC].canary}"}
    )
    return source


def _grant() -> AuthorityGrant:
    return AuthorityGrant(
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


def _run(plan, tmp_path):
    source = _source()
    snapshot = get_permission_snapshot(SEEDED_USER_ID, captured_at=T0)
    log = tmp_path / "decisions.jsonl"
    attempt, authorization, record = authorize_and_record(
        _grant(),
        plan,
        now=NOW,
        permission_snapshot=snapshot,
        agent_capabilities=get_agent_capabilities(SEEDED_AGENT_ID),
        documents=source,
        decision_log_path=log,
    )
    artifacts = {
        "effect": attempt.effect.model_dump_json(),
        "policy_context": LegalPolicyContext(grant=_grant(), effect=attempt.effect).model_dump_json(),
        "rule_details": b"".join(
            canonical_json(dict(outcome.details)) for outcome in attempt.decision.outcomes
        ).decode(),
        "permission_checks": attempt.permissions.model_dump_json(),
        "legal_action": LegalAction(
            grant=_grant(), plan=plan, effect=attempt.effect, permission_snapshot=snapshot
        ).model_dump_json(),
        "decision_record": record.model_dump_json(),
        "persisted_jsonl": log.read_text(encoding="utf-8"),
    }
    if authorization is not None:
        artifacts["authorization_record"] = authorization.model_dump_json()
    return source, attempt, authorization, artifacts


def _forbidden_strings(source) -> list[str]:
    restricted_canaries = [document.canary for document in build_corpus() if document.canary]
    return [
        PLAIN_SENTINEL,
        RESTRICTED_SENTINEL,
        "SENTINEL-",
        "Acquisition codename",
        "Synthetic text",
        *restricted_canaries,
        *(document.text for document in source.values()),
    ]


def test_sentinels_exist_in_the_source_documents():
    source = _source()

    assert source[PLAIN_DOC].text == PLAIN_SENTINEL
    assert RESTRICTED_SENTINEL in source[RESTRICTED_DOC].text
    assert source[RESTRICTED_DOC].restricted is True


@pytest.mark.parametrize("plan,verdict", [(CLEAN_PLAN, "ALLOW"), (CANONICAL_PLAN, "BLOCK")], ids=["allow", "block"])
def test_no_text_or_canary_reaches_any_artifact(tmp_path, plan, verdict):
    source, attempt, authorization, artifacts = _run(plan, tmp_path)

    assert attempt.verdict == verdict
    assert ("authorization_record" in artifacts) is (verdict == "ALLOW")
    for name, serialized in artifacts.items():
        for forbidden in _forbidden_strings(source):
            assert forbidden not in serialized, (name, forbidden[:40])


def test_permitted_metadata_and_hashes_survive(tmp_path):
    source, attempt, _, artifacts = _run(CANONICAL_PLAN, tmp_path)
    facts = {fact.doc_id: fact for fact in attempt.effect.consequential_resources}

    restricted = facts[RESTRICTED_DOC]
    assert (restricted.matter_id, restricted.privileged, restricted.restricted) == ("M-102", False, True)
    assert restricted.content_hash == sha256_digest(source[RESTRICTED_DOC].text)
    assert facts[PLAIN_DOC].content_hash == sha256_digest(PLAIN_SENTINEL)
    assert RESTRICTED_DOC in artifacts["effect"] and RESTRICTED_DOC in artifacts["legal_action"]
    assert '"restricted_count":4' in artifacts["decision_record"]
    assert '"M-118":17' in artifacts["decision_record"]


# ---------------------------------------------------------------------------
# Structural guards
# ---------------------------------------------------------------------------

LEGAL_SOURCES = sorted(Path(legal.__file__).parent.glob("*.py")) + sorted(
    Path(legal_binding.__file__).parent.glob("*.py")
)
REPO_ROOT = Path(legal.__file__).resolve().parent.parent


class _RawTextAccess(ast.NodeVisitor):
    def __init__(self) -> None:
        self.functions: list[str] = []
        self.found: list[tuple[str | None, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in {"text", "canary"}:
            self.found.append((self.functions[-1] if self.functions else None, node.attr))
        self.generic_visit(node)


def _raw_text_accesses(source: str) -> list[tuple[str | None, str]]:
    visitor = _RawTextAccess()
    visitor.visit(ast.parse(source))
    return visitor.found


def test_raw_text_is_read_only_to_compute_the_content_hash():
    accesses = {
        (path.relative_to(REPO_ROOT).as_posix(), function, attribute)
        for path in LEGAL_SOURCES
        for function, attribute in _raw_text_accesses(path.read_text())
    }

    assert accesses == {("legal/models.py", "content_hash", "text")}


def test_raw_text_guard_catches_a_downstream_read():
    leak = "def resolve_plan(plan, documents):\n    return [d.text for d in documents.values()]\n"

    assert _raw_text_accesses(leak) == [("resolve_plan", "text")]


def test_no_legal_action_boundary_module_imports_a_model_client():
    model_clients = ("openai", "anthropic", "agent", "craft", "mcp", "requests", "httpx", "urllib")
    for path in LEGAL_SOURCES:
        imported = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not [module for module in imported if module.split(".")[0] in model_clients], path.name
