"""Non-UI helper functions for the ProofGate Streamlit demo (app.py).

Kept separate from app.py -- which contains only Streamlit widgets/layout
-- so these functions are unit-testable without importing streamlit or
running `streamlit run`. Nothing here talks to Streamlit; everything here
reads/writes only the existing real backend (proofgate.*, operations.*,
craft.*).
"""

import json
import shutil
from pathlib import Path

from agent import nebius_client
from operations.database import reset_working_db
from proofgate.audit import reset_audit_log
from proofgate.budgets import reset_workflow_state
from proofgate.models import CraftEvidence, RollbackProof, TriggeredRule

FIXED_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."
WORKFLOW_ID = "demo-workflow"

# Fixed for this deterministic demo: the corrected call's real test-user
# count under the seeded/reset database.
CORRECTED_INACTIVE_DAYS = 90
CORRECTED_MAX_AFFECTED_ROWS = 92

RESET_SUCCESS_MESSAGE = "Demo reset. Database reseeded and workflow state cleared."

# A real, previously-retrieved live CRAFT result, committed to the repo so
# a fresh checkout still has cached evidence available without ever
# running live OAuth. artifacts/craft_evidence_cache.json itself stays
# gitignored (it's runtime output); this is the durable source copy.
DEMO_SEED_DIR = Path(__file__).resolve().parent / "demo_seed"
CRAFT_EVIDENCE_SEED_PATH = DEMO_SEED_DIR / "craft_evidence_seed.json"


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


def ensure_craft_cache_seeded(
    seed_path: Path = CRAFT_EVIDENCE_SEED_PATH, cache_path: Path | None = None
) -> bool:
    """Copy the committed, real previously-retrieved CRAFT evidence into the
    runtime cache path if nothing is cached there yet. Never overwrites an
    existing cache file (a genuine live run always takes precedence).
    Returns True if a copy was made. No-op (returns False) if the seed file
    doesn't exist or the cache is already populated.
    """
    if cache_path is None:
        from craft.config import cache_path as _cache_path

        cache_path = _cache_path()

    if cache_path.exists() or not seed_path.exists():
        return False

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(seed_path, cache_path)
    return True


def operations_db_status_label(db_path: Path) -> str:
    """Static, no-network status label for the demo status area."""
    return "Ready" if db_path.exists() else "Not initialized"


def craft_status_label(mode: str | None) -> str:
    """Static status label derived from an already-computed CraftEvidenceOutcome
    mode -- never triggers a new CRAFT call."""
    if mode == "live":
        return "Live"
    if mode == "cached":
        return "Cached"
    return "Unavailable"


_AUDIT_CARD_FIELDS = (
    ("verdict", "Verdict"),
    ("affected_count", "Affected count"),
    ("production_affected", "Production affected"),
    ("test_affected", "Test affected"),
    ("executed", "Executed"),
    ("proof_status", "Proof status"),
    ("postcondition_status", "Postcondition status"),
)


def latest_event_by_verdict(rows: list[dict], verdict: str) -> dict | None:
    """Most recent audit row matching verdict (BLOCK or ALLOW), or None.
    rows is expected in file order (oldest first), matching
    read_audit_rows' output -- the last match is the most recent one."""
    matches = [row for row in rows if row.get("verdict") == verdict]
    return matches[-1] if matches else None


def audit_card_fields(row: dict | None) -> dict[str, str]:
    """Plain-string fields for one compact audit card (verdict, affected
    count, production affected, test affected, executed, proof status,
    postcondition status). Every value is converted to a plain str (or
    em-dash for None/missing) before this ever reaches a Streamlit UI
    component -- never a Pydantic model, enum, datetime, or Pandas/NumPy
    value, and never a full row/table object.
    """
    fields: dict[str, str] = {}
    for key, label in _AUDIT_CARD_FIELDS:
        value = row.get(key) if row is not None else None
        if value is None:
            fields[label] = "—"
        elif isinstance(value, bool):
            fields[label] = "Yes" if value else "No"
        else:
            fields[label] = str(value)
    return fields


def recovery_proof_card_fields(
    proof_status: str, proof: RollbackProof | None, proof_checks: dict | None
) -> dict[str, str]:
    """Plain-string fields for the recovery-proof verification card (Slice
    15): validation status, snapshot ID, protected resource, selector hash,
    maximum affected rows, and the four real proof_checks booleans.

    Never fabricates a snapshot/resource/selector/max-rows when no
    RollbackProof was actually supplied for this call (the unsafe path
    passes rollback_proof=None to the real backend) -- those fields
    honestly read "No rollback proof supplied" instead. proof_status and
    proof_checks always come from the real AuditEvent regardless.
    """
    fields: dict[str, str] = {"Validation status": proof_status}

    if proof is not None:
        fields["Snapshot ID"] = proof.snapshot_id
        fields["Protected resource"] = proof.resource
        fields["Selector hash"] = proof.selector_hash
        fields["Maximum affected rows"] = str(proof.max_affected_rows)
    else:
        fields["Snapshot ID"] = "No rollback proof supplied"
        fields["Protected resource"] = "—"
        fields["Selector hash"] = "—"
        fields["Maximum affected rows"] = "—"

    for check_key, label in (
        ("snapshot_exists", "Snapshot exists"),
        ("resource_matches", "Resource matches"),
        ("selector_hash_matches", "Selector hash matches"),
        ("count_within_approved_maximum", "Count within approved maximum"),
    ):
        value = (proof_checks or {}).get(check_key)
        if value is True:
            fields[label] = "✓"
        elif value is False:
            fields[label] = "✗"
        else:
            fields[label] = "—"

    return fields


def nebius_status_label() -> str:
    """Static, no-network status label: reports configuration, not an
    actual live probe. 'Live (configured)' means live extraction will be
    attempted on the next action; the real per-call outcome (nebius_live /
    mixed / deterministic_fallback) is recorded in each audit event."""
    if nebius_client.live_enabled() and nebius_client.has_api_key():
        return "Live (configured)"
    return "Fallback (deterministic)"
