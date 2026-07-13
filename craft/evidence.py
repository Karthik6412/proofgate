"""CRAFT evidence preparation: real live workflow with a clearly labeled
cached fallback.

CRAFT is upstream, read-only enterprise context. It never performs,
authorizes, previews, or influences the operational deletion; its
evidence is never authoritative for mutation impact. This module is
never imported by proofgate.core / guarded_delete_users -- the future
orchestrator/UI calls prepare_craft_evidence before the guarded call, and
guarded_delete_users only retrieves whatever was already stored, via
proofgate.budgets.get_craft_evidence.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from craft import config
from craft.client import CraftWorkflowError, run_craft_workflow, unwrap_exception_group
from proofgate.budgets import record_craft_evidence
from proofgate.models import CraftEvidence

LABEL_LIVE = "CRAFT enterprise evidence"
LABEL_CACHED = "Previously retrieved CRAFT evidence"

DEMO_COHORT_QUESTION = (
    "Using the available e-commerce schema, count customers whose most "
    "recent order is at least 90 days before the latest order date "
    "represented in the dataset. Return a small read-only cohort summary."
)

_SANITIZE_MARKERS = (
    "authorization:",
    "bearer ",
    "token=",
    "client_secret",
    "x-project-id",
    "api_key",
    "apikey",
)

_REDACTED = "(details withheld to avoid leaking credentials)"


@dataclass
class CraftEvidenceOutcome:
    """Internal runtime metadata, not a wire contract."""

    evidence: CraftEvidence | None
    mode: Literal["live", "cached", "unavailable"]
    error_summary: str | None


def _sanitize_text(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in _SANITIZE_MARKERS):
        return _REDACTED
    return text[:300]


def _sanitize_error(exc: Exception, diagnostics: dict | None = None) -> str:
    """Unwrap TaskGroup/ExceptionGroup wrappers down to the real underlying
    failure and report [stage] ExceptionType: sanitized message, instead of
    the generic "unhandled errors in a TaskGroup" wrapper text."""
    leaf = unwrap_exception_group(exc)
    stage = (diagnostics or {}).get("current_stage", "unknown_stage")
    exc_type = type(leaf).__name__
    message = _sanitize_text(str(leaf))
    return f"[{stage}] {exc_type}: {message}"


def _load_cache(path: Path) -> CraftEvidence | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        return CraftEvidence.model_validate(raw)
    except Exception:
        return None


def _save_cache(path: Path, evidence: CraftEvidence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(evidence.model_dump_json(indent=2))


def prepare_craft_evidence(
    workflow_id: str,
    original_instruction: str,
    question: str = DEMO_COHORT_QUESTION,
    session_factory=None,
    diagnostics: dict | None = None,
    force_fallback: bool = False,
) -> CraftEvidenceOutcome:
    """Attempt the live CRAFT workflow when enabled; fall back to clearly
    labeled cached evidence on failure. Records the outcome in
    WorkflowState by workflow_id. Never called from guarded_delete_users.

    force_fallback (Slice 20.1) unconditionally skips the live branch,
    regardless of config.live_enabled()/has_project_id() or session_factory.
    Used for an automatic, ungated call site (e.g. Streamlit's page-load
    evidence panel) that must be structurally incapable of attempting a
    live call -- not merely configured not to -- so it can never initiate
    OAuth no matter what CRAFT_LIVE_ENABLED/CRAFT_PROJECT_ID happen to be
    set to at that moment. An explicit, user-initiated call (e.g. a
    "Fetch live CRAFT evidence" button) omits this flag so it can attempt
    a real live call when configured.
    """
    cache_path = config.cache_path()
    error_summary = None
    if diagnostics is None:
        diagnostics = {}

    should_attempt_live = not force_fallback and (
        session_factory is not None or (config.live_enabled() and config.has_project_id())
    )

    if should_attempt_live:
        try:
            result = run_craft_workflow(question, session_factory=session_factory, diagnostics=diagnostics)
            evidence = CraftEvidence(
                label=LABEL_LIVE,
                mode="live",
                database=result.database,
                question=result.question,
                generated_sql=result.generated_sql,
                result_summary=result.result_summary,
                result_preview=result.result_preview,
                tool_trace=result.tool_trace,
                retrieved_at=datetime.now(timezone.utc).isoformat(),
                authoritative_for_mutation_impact=False,
            )
            _save_cache(cache_path, evidence)
            record_craft_evidence(workflow_id, evidence)
            return CraftEvidenceOutcome(evidence=evidence, mode="live", error_summary=None)
        except Exception as exc:  # noqa: BLE001 -- any failure must fall back, never crash
            error_summary = _sanitize_error(exc, diagnostics)

    cached = _load_cache(cache_path)
    if cached is not None:
        relabeled = cached.model_copy(update={"label": LABEL_CACHED, "mode": "cached"})
        record_craft_evidence(workflow_id, relabeled)
        return CraftEvidenceOutcome(evidence=relabeled, mode="cached", error_summary=error_summary)

    record_craft_evidence(workflow_id, None)
    return CraftEvidenceOutcome(evidence=None, mode="unavailable", error_summary=error_summary)
