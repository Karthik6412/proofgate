"""Non-UI helper functions for the ProofGate Streamlit demo (app.py).

Kept separate from app.py -- which contains only Streamlit widgets/layout
-- so these functions are unit-testable without importing streamlit or
running `streamlit run`. Nothing here talks to Streamlit; everything here
reads/writes only the existing real backend (proofgate.*, operations.*,
craft.*).
"""

import json
from pathlib import Path

from operations.database import reset_working_db
from proofgate.audit import reset_audit_log
from proofgate.budgets import reset_workflow_state
from proofgate.models import CraftEvidence, TriggeredRule

FIXED_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."
WORKFLOW_ID = "demo-workflow"

# Fixed for this deterministic demo: the corrected call's real test-user
# count under the seeded/reset database.
CORRECTED_INACTIVE_DAYS = 90
CORRECTED_MAX_AFFECTED_ROWS = 92


def triggered_rule_rows(triggered_rules: list[TriggeredRule]) -> list[dict]:
    """Table-ready rows for the triggered deterministic policy rules."""
    return [{"rule_id": rule.rule_id, "explanation": rule.explanation} for rule in triggered_rules]


def craft_evidence_display(evidence: CraftEvidence | None) -> dict | None:
    """Extract only the fields the CRAFT evidence panel may show.

    Returns None when there is no evidence to display. show_result_preview
    is False whenever the bounded preview came back empty -- the panel
    must never render an empty raw preview, even when result_summary
    describes rows having been returned.
    """
    if evidence is None:
        return None
    return {
        "label": evidence.label,
        "mode": evidence.mode,
        "database": evidence.database,
        "question": evidence.question,
        "generated_sql": evidence.generated_sql,
        "result_summary": evidence.result_summary,
        "result_preview": evidence.result_preview,
        "show_result_preview": bool(evidence.result_preview),
        "tool_trace": evidence.tool_trace,
        "retrieved_at": evidence.retrieved_at,
    }


def audit_event_row(raw_event: dict) -> dict:
    """Reduce one raw AuditEvent JSON object to the compact fields the
    audit table is allowed to show -- no risk/intent/CRAFT internals.

    Affected counts come from mutation_result when it's present (ALLOW --
    the actual post-execution counts), and fall back to the preflight
    impact_envelope counts when mutation_result is absent (BLOCK -- nothing
    executed, so the real backend never produced a MutationResult; the
    only real counts available are the preflight ones). If neither is
    present (e.g. RULE_UNKNOWN_IMPACT, preview itself failed), all three
    counts are None rather than fabricated.
    """
    tool_arguments = raw_event.get("tool_arguments") or {}
    mutation_result = raw_event.get("mutation_result") or {}
    impact_envelope = raw_event.get("impact_envelope") or {}
    environment_counts = impact_envelope.get("environment_counts") or {}
    postcondition_result = raw_event.get("postcondition_result") or {}

    if mutation_result:
        affected_count = mutation_result.get("affected_count")
        production_affected = mutation_result.get("production_affected")
        test_affected = mutation_result.get("test_affected")
    else:
        affected_count = impact_envelope.get("estimated_count")
        production_affected = environment_counts.get("production")
        test_affected = environment_counts.get("test")

    return {
        "event_id": raw_event.get("event_id"),
        "verdict": raw_event.get("verdict"),
        "tool": raw_event.get("tool_name"),
        "environment": tool_arguments.get("environment"),
        "inactive_days": tool_arguments.get("inactive_days"),
        "affected_count": affected_count,
        "production_affected": production_affected,
        "test_affected": test_affected,
        "executed": raw_event.get("execution_status") == "EXECUTED",
        "timestamp": raw_event.get("timestamp"),
        "proof_status": raw_event.get("proof_status"),
        "postcondition_status": postcondition_result.get("status"),
    }


def format_count(value) -> str:
    """Format an integer count with thousands separators (e.g. 10073 ->
    "10,073"). Returns an em dash for missing/non-numeric values."""
    if isinstance(value, bool) or not isinstance(value, int):
        return "—"
    return f"{value:,}"


def format_executed(executed: bool) -> str:
    """Judge-readable Yes/No in place of lowercase true/false."""
    return "Yes" if executed else "No"


def _read_audit_events(audit_path: Path) -> list[dict]:
    if not audit_path.exists():
        return []
    events = []
    for line in audit_path.read_text().strip().splitlines():
        if not line:
            continue
        events.append(json.loads(line))
    return events


def read_audit_rows(audit_path: Path, workflow_id: str | None = None) -> list[dict]:
    """Read the JSONL audit log and return compact display rows, optionally
    filtered to one workflow_id. Returns [] if the file doesn't exist yet."""
    rows = []
    for raw_event in _read_audit_events(audit_path):
        if workflow_id is not None and raw_event.get("workflow_id") != workflow_id:
            continue
        rows.append(audit_event_row(raw_event))
    return rows


def find_audit_event_by_id(audit_path: Path, event_id: str) -> dict | None:
    """Find the raw AuditEvent JSON object matching event_id, or None.

    Used to display the exact recorded impact/mutation/postcondition data
    for a given EnforcementResult, rather than re-deriving it separately.
    """
    for raw_event in _read_audit_events(audit_path):
        if raw_event.get("event_id") == event_id:
            return raw_event
    return None


def reset_demo_state(workflow_id: str = WORKFLOW_ID) -> None:
    """Reset the operations database, this workflow's ProofGate runtime
    state, and the demo audit log -- returns the backend to a clean
    initial state. Never touches operations/pristine.db or the CRAFT
    evidence cache file."""
    reset_working_db()
    reset_workflow_state(workflow_id)
    reset_audit_log()
