"""Tests for the non-UI helper functions backing the Streamlit demo
(app_logic.py). app.py itself contains only Streamlit widgets/layout and
is intentionally not imported here -- see test_app_import.py for the
plain import check instead.

tests/conftest.py's autouse fixtures already isolate the audit log and
CRAFT cache path per test; these tests additionally pass explicit
tmp_path files to the app_logic functions that take a path parameter, so
none of them depend on that global isolation to be correct.
"""

import json
from pathlib import Path

from app_logic import (
    DEACTIVATE_WORKFLOW_ID,
    audit_card_fields,
    audit_event_row,
    comparison_markdown_table,
    craft_evidence_display,
    craft_evidence_source_label,
    craft_source_label,
    craft_status_label,
    ensure_craft_cache_seeded,
    find_audit_event_by_id,
    format_count,
    format_executed,
    latest_event_by_verdict,
    mutation_verb_label,
    nebius_source_label,
    nebius_status_label,
    operations_db_status_label,
    policy_rule_comparison_rows,
    read_audit_rows,
    recovery_proof_card_fields,
    recovery_proof_requirement_label,
    reset_deactivate_demo_state,
    reset_demo_state,
    triggered_rule_rows,
)
from operations.actions import count_rows, preview_delete_users
from operations.database import WORKING_DB_PATH
from proofgate.budgets import get_workflow_budget
from proofgate.core import guarded_delete_users, guarded_execute
from proofgate.models import ActionContext, CraftEvidence, RollbackProof, TriggeredRule

WORKFLOW_ID = "test-demo-workflow"


def _action_context() -> ActionContext:
    return ActionContext(
        workflow_id=WORKFLOW_ID,
        requesting_user="tester",
        agent_id="tester-agent",
        original_instruction="Clean up inactive test accounts that have not logged in for 90 days.",
    )


# ---------------------------------------------------------------------------
# triggered_rule_rows
# ---------------------------------------------------------------------------


def test_triggered_rule_rows_maps_fields():
    rules = [
        TriggeredRule(rule_id="RULE_A", explanation="explanation A"),
        TriggeredRule(rule_id="RULE_B", explanation="explanation B"),
    ]
    assert triggered_rule_rows(rules) == [
        {"rule_id": "RULE_A", "explanation": "explanation A"},
        {"rule_id": "RULE_B", "explanation": "explanation B"},
    ]


def test_triggered_rule_rows_empty_list():
    assert triggered_rule_rows([]) == []


# ---------------------------------------------------------------------------
# craft_evidence_display
# ---------------------------------------------------------------------------


def _make_evidence(result_preview, result_summary="Returned 1 row(s) (showing up to 5)."):
    return CraftEvidence(
        label="CRAFT enterprise evidence",
        mode="cached",
        database="thelook-ecommerce-0f0a359c",
        question="some cohort question",
        generated_sql="SELECT 1",
        result_summary=result_summary,
        result_preview=result_preview,
        tool_trace=["list_tools", "generate_sql", "execute_query"],
        retrieved_at="2026-07-11T00:00:00+00:00",
        authoritative_for_mutation_impact=False,
    )


def test_craft_evidence_display_returns_none_for_none():
    assert craft_evidence_display(None) is None


def test_craft_evidence_display_hides_preview_when_empty_but_summary_says_rows():
    # The exact edge case this task calls out: result_preview empty, but
    # result_summary claims a row was returned.
    evidence = _make_evidence(result_preview=[], result_summary="Returned 1 row(s) (showing up to 5).")
    display = craft_evidence_display(evidence)
    assert display["show_result_preview"] is False
    assert display["result_preview"] == []
    assert display["result_summary"] == "Returned 1 row(s) (showing up to 5)."


def test_craft_evidence_display_shows_preview_when_present():
    evidence = _make_evidence(result_preview=[{"cohort_count": 42}])
    display = craft_evidence_display(evidence)
    assert display["show_result_preview"] is True
    assert display["result_preview"] == [{"cohort_count": 42}]


def test_craft_evidence_display_includes_expected_fields_only():
    evidence = _make_evidence(result_preview=[{"n": 1}])
    display = craft_evidence_display(evidence)
    assert set(display.keys()) == {
        "label",
        "mode",
        "database",
        "question",
        "generated_sql",
        "result_summary",
        "result_preview",
        "show_result_preview",
        "tool_trace",
        "retrieved_at",
    }


# ---------------------------------------------------------------------------
# audit_event_row
# ---------------------------------------------------------------------------


def _raw_block_event():
    return {
        "event_id": "evt-block-1",
        "workflow_id": WORKFLOW_ID,
        "verdict": "BLOCK",
        "tool_name": "delete_users",
        "tool_arguments": {"inactive_days": 90, "environment": None},
        "impact_envelope": {
            "tool_name": "delete_users",
            "estimated_count": 10073,
            "environment_counts": {"production": 9981, "test": 92},
            "hard_delete": True,
            "reversibility": "irreversible_without_snapshot",
            "selector_hash": "deadbeef",
            "generated_at": "2026-07-11T00:00:00+00:00",
        },
        "mutation_result": None,
        "postcondition_result": None,
        "execution_status": "NOT_EXECUTED",
        "timestamp": "2026-07-11T00:00:00+00:00",
        "proof_status": "MISSING",
    }


def _raw_allow_event():
    return {
        "event_id": "evt-allow-1",
        "workflow_id": WORKFLOW_ID,
        "verdict": "ALLOW",
        "tool_name": "delete_users",
        "tool_arguments": {"inactive_days": 90, "environment": "test"},
        "mutation_result": {"affected_count": 92, "production_affected": 0, "test_affected": 92},
        "postcondition_result": {
            "predicted_count": 92,
            "actual_count": 92,
            "production_affected": 0,
            "status": "VERIFIED",
        },
        "execution_status": "EXECUTED",
        "timestamp": "2026-07-11T00:01:00+00:00",
        "proof_status": "VALID",
    }


def test_audit_event_row_maps_block_event_using_impact_envelope_counts():
    # BLOCK never produces a MutationResult, so the audit row's counts must
    # come from the real preflight impact_envelope instead -- matching
    # exactly what the BLOCK result card already displays.
    row = audit_event_row(_raw_block_event())
    assert row == {
        "event_id": "evt-block-1",
        "verdict": "BLOCK",
        "tool": "delete_users",
        "environment": None,
        "inactive_days": 90,
        "affected_count": 10073,
        "production_affected": 9981,
        "test_affected": 92,
        "executed": False,
        "timestamp": "2026-07-11T00:00:00+00:00",
        "proof_status": "MISSING",
        "postcondition_status": None,
    }


def test_audit_event_row_missing_impact_and_mutation_fails_gracefully():
    # Neither mutation_result nor impact_envelope present at all (e.g. the
    # RULE_UNKNOWN_IMPACT path, where preview itself failed) -- counts must
    # be None, never fabricated or defaulted to zero.
    event = _raw_block_event()
    event["impact_envelope"] = None
    row = audit_event_row(event)
    assert row["affected_count"] is None
    assert row["production_affected"] is None
    assert row["test_affected"] is None


def test_audit_event_row_maps_allow_event():
    row = audit_event_row(_raw_allow_event())
    assert row == {
        "event_id": "evt-allow-1",
        "verdict": "ALLOW",
        "tool": "delete_users",
        "environment": "test",
        "inactive_days": 90,
        "affected_count": 92,
        "production_affected": 0,
        "test_affected": 92,
        "executed": True,
        "timestamp": "2026-07-11T00:01:00+00:00",
        "proof_status": "VALID",
        "postcondition_status": "VERIFIED",
    }


# ---------------------------------------------------------------------------
# read_audit_rows / find_audit_event_by_id
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event))
            f.write("\n")


def test_read_audit_rows_returns_empty_list_when_file_missing(tmp_path):
    assert read_audit_rows(tmp_path / "does-not-exist.jsonl") == []


def test_read_audit_rows_returns_all_events_when_no_filter(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_jsonl(path, [_raw_block_event(), _raw_allow_event()])

    rows = read_audit_rows(path)
    assert len(rows) == 2
    assert rows[0]["verdict"] == "BLOCK"
    assert rows[1]["verdict"] == "ALLOW"


def test_read_audit_rows_filters_by_workflow_id(tmp_path):
    path = tmp_path / "audit.jsonl"
    other_workflow_event = dict(_raw_block_event(), workflow_id="some-other-workflow")
    _write_jsonl(path, [_raw_block_event(), other_workflow_event])

    rows = read_audit_rows(path, workflow_id=WORKFLOW_ID)
    assert len(rows) == 1
    assert rows[0]["event_id"] == "evt-block-1"


def test_find_audit_event_by_id_finds_matching_event(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_jsonl(path, [_raw_block_event(), _raw_allow_event()])

    found = find_audit_event_by_id(path, "evt-allow-1")
    assert found is not None
    assert found["verdict"] == "ALLOW"


def test_find_audit_event_by_id_returns_none_when_not_found(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_jsonl(path, [_raw_block_event()])

    assert find_audit_event_by_id(path, "evt-nonexistent") is None


def test_find_audit_event_by_id_returns_none_when_file_missing(tmp_path):
    assert find_audit_event_by_id(tmp_path / "missing.jsonl", "evt-1") is None


# ---------------------------------------------------------------------------
# reset_demo_state (integration: real backend, isolated audit/db paths)
# ---------------------------------------------------------------------------


def test_reset_demo_state_resets_budget_and_audit_log(monkeypatch, tmp_path):
    import proofgate.audit as audit_module

    monkeypatch.setattr(audit_module, "DEFAULT_AUDIT_PATH", tmp_path / "audit.jsonl")

    # Populate real state: one BLOCK evaluation for this workflow_id.
    guarded_delete_users(
        action_context=_action_context(),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )
    assert get_workflow_budget(WORKFLOW_ID).rows_mutated == 0  # BLOCK never mutates budget
    assert len(read_audit_rows(audit_module.DEFAULT_AUDIT_PATH)) == 1

    reset_demo_state(WORKFLOW_ID)

    assert get_workflow_budget(WORKFLOW_ID).rows_mutated == 0
    assert read_audit_rows(audit_module.DEFAULT_AUDIT_PATH) == []


def test_reset_demo_state_restores_pristine_row_counts(monkeypatch, tmp_path):
    import proofgate.audit as audit_module

    monkeypatch.setattr(audit_module, "DEFAULT_AUDIT_PATH", tmp_path / "audit.jsonl")

    reset_demo_state(WORKFLOW_ID)
    before = count_rows(WORKING_DB_PATH)
    assert before == 10623

    impact = preview_delete_users(inactive_days=90, environment=None)
    assert impact.estimated_count == 10073
    assert impact.environment_counts == {"production": 9981, "test": 92}


# ---------------------------------------------------------------------------
# format_count / format_executed
# ---------------------------------------------------------------------------


def test_format_count_adds_thousands_separators():
    assert format_count(10073) == "10,073"
    assert format_count(9981) == "9,981"
    assert format_count(92) == "92"
    assert format_count(0) == "0"


def test_format_count_returns_em_dash_for_missing_or_non_numeric():
    assert format_count(None) == "—"
    assert format_count("not-a-number") == "—"
    assert format_count(True) == "—"  # bool is technically an int -- must not format as 1/0


def test_format_executed_yes_for_true_no_for_false():
    assert format_executed(True) == "Yes"
    assert format_executed(False) == "No"


# ---------------------------------------------------------------------------
# ensure_craft_cache_seeded
# ---------------------------------------------------------------------------


def test_ensure_craft_cache_seeded_copies_seed_when_cache_missing(tmp_path):
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps({"label": "CRAFT enterprise evidence", "mode": "live"}))
    cache_path = tmp_path / "nested" / "cache.json"

    copied = ensure_craft_cache_seeded(seed_path=seed_path, cache_path=cache_path)

    assert copied is True
    assert cache_path.exists()
    assert json.loads(cache_path.read_text())["label"] == "CRAFT enterprise evidence"


def test_ensure_craft_cache_seeded_never_overwrites_existing_cache(tmp_path):
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps({"mode": "live", "marker": "seed"}))
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"mode": "cached", "marker": "real-run"}))

    copied = ensure_craft_cache_seeded(seed_path=seed_path, cache_path=cache_path)

    assert copied is False
    assert json.loads(cache_path.read_text())["marker"] == "real-run"


def test_ensure_craft_cache_seeded_no_op_when_seed_missing(tmp_path):
    seed_path = tmp_path / "does-not-exist.json"
    cache_path = tmp_path / "cache.json"

    copied = ensure_craft_cache_seeded(seed_path=seed_path, cache_path=cache_path)

    assert copied is False
    assert not cache_path.exists()


def test_committed_craft_evidence_seed_file_is_a_valid_craft_evidence():
    # The actual committed seed file used in production must itself be a
    # valid, real CraftEvidence document -- not a placeholder.
    from app_logic import CRAFT_EVIDENCE_SEED_PATH
    from proofgate.models import CraftEvidence

    raw = json.loads(CRAFT_EVIDENCE_SEED_PATH.read_text())
    evidence = CraftEvidence.model_validate(raw)
    assert evidence.authoritative_for_mutation_impact is False


# ---------------------------------------------------------------------------
# Demo status labels (no network calls)
# ---------------------------------------------------------------------------


def test_operations_db_status_label_ready_when_file_exists(tmp_path):
    db_path = tmp_path / "working.db"
    db_path.write_text("x")
    assert operations_db_status_label(db_path) == "Ready"


def test_operations_db_status_label_not_initialized_when_missing(tmp_path):
    assert operations_db_status_label(tmp_path / "missing.db") == "Not initialized"


def test_craft_status_label_maps_modes():
    assert craft_status_label("live") == "Live"
    assert craft_status_label("cached") == "Cached"
    assert craft_status_label("unavailable") == "Unavailable"
    assert craft_status_label(None) == "Unavailable"


def test_nebius_status_label_reflects_configuration(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")
    assert nebius_status_label() == "Live (configured)"

    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "false")
    assert nebius_status_label() == "Fallback (deterministic)"

    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    assert nebius_status_label() == "Fallback (deterministic)"


# ---------------------------------------------------------------------------
# latest_event_by_verdict / audit_card_fields (segfault-fix replacement for
# st.dataframe/st.table -- these feed the two compact native audit cards)
# ---------------------------------------------------------------------------


def test_latest_event_by_verdict_returns_most_recent_match():
    rows = [
        {"verdict": "BLOCK", "event_id": "evt-1"},
        {"verdict": "ALLOW", "event_id": "evt-2"},
        {"verdict": "BLOCK", "event_id": "evt-3"},
    ]
    assert latest_event_by_verdict(rows, "BLOCK")["event_id"] == "evt-3"
    assert latest_event_by_verdict(rows, "ALLOW")["event_id"] == "evt-2"


def test_latest_event_by_verdict_returns_none_when_no_match():
    rows = [{"verdict": "BLOCK", "event_id": "evt-1"}]
    assert latest_event_by_verdict(rows, "ALLOW") is None


def test_latest_event_by_verdict_returns_none_for_empty_rows():
    assert latest_event_by_verdict([], "BLOCK") is None


def test_audit_card_fields_returns_only_plain_strings():
    row = {
        "verdict": "BLOCK",
        "affected_count": 10073,
        "production_affected": 9981,
        "test_affected": 92,
        "executed": False,
        "proof_status": "MISSING",
        "postcondition_status": None,
    }
    fields = audit_card_fields(row)

    assert fields == {
        "Verdict": "BLOCK",
        "Affected count": "10073",
        "Production affected": "9981",
        "Test affected": "92",
        "Executed": "No",
        "Proof status": "MISSING",
        "Postcondition status": "—",
    }
    for value in fields.values():
        assert isinstance(value, str)


def test_audit_card_fields_formats_executed_true_as_yes():
    row = {
        "verdict": "ALLOW",
        "affected_count": 92,
        "production_affected": 0,
        "test_affected": 92,
        "executed": True,
        "proof_status": "VALID",
        "postcondition_status": "VERIFIED",
    }
    fields = audit_card_fields(row)
    assert fields["Executed"] == "Yes"
    assert fields["Postcondition status"] == "VERIFIED"


def test_audit_card_fields_handles_none_row_with_all_placeholders():
    fields = audit_card_fields(None)
    assert all(value == "—" for value in fields.values())
    assert set(fields.keys()) == {
        "Verdict",
        "Affected count",
        "Production affected",
        "Test affected",
        "Executed",
        "Proof status",
        "Postcondition status",
    }


def test_audit_card_fields_no_pydantic_or_nested_objects_leak_through():
    # Defense-in-depth: even if a caller accidentally passed a row still
    # containing a nested object under an unrelated key, audit_card_fields
    # must only ever emit strings for the fixed set of fields it reads.
    row = {
        "verdict": "BLOCK",
        "affected_count": 10073,
        "production_affected": 9981,
        "test_affected": 92,
        "executed": False,
        "proof_status": "MISSING",
        "postcondition_status": None,
        "some_other_nested_field": object(),
    }
    fields = audit_card_fields(row)
    assert all(isinstance(v, str) for v in fields.values())


# ---------------------------------------------------------------------------
# recovery_proof_card_fields (Slice 15: proof verification card)
# ---------------------------------------------------------------------------


def test_recovery_proof_card_fields_with_no_proof_supplied():
    # The unsafe path passes rollback_proof=None to the real backend --
    # nothing must be fabricated for snapshot/resource/selector/max-rows.
    fields = recovery_proof_card_fields("MISSING", None, None)
    assert fields["Validation status"] == "MISSING"
    assert fields["Snapshot ID"] == "No rollback proof supplied"
    assert fields["Protected resource"] == "—"
    assert fields["Selector hash"] == "—"
    assert fields["Maximum affected rows"] == "—"
    assert fields["Snapshot exists"] == "—"
    assert fields["Resource matches"] == "—"
    assert fields["Selector hash matches"] == "—"
    assert fields["Count within approved maximum"] == "—"


def test_recovery_proof_card_fields_with_valid_proof_and_checks():
    proof = RollbackProof(
        snapshot_id="snap-abc123",
        resource="users",
        selector_hash="deadbeef",
        max_affected_rows=92,
    )
    proof_checks = {
        "snapshot_exists": True,
        "resource_matches": True,
        "selector_hash_matches": True,
        "count_within_approved_maximum": True,
    }
    fields = recovery_proof_card_fields("VALID", proof, proof_checks)

    assert fields["Validation status"] == "VALID"
    assert fields["Snapshot ID"] == "snap-abc123"
    assert fields["Protected resource"] == "users"
    assert fields["Selector hash"] == "deadbeef"
    assert fields["Maximum affected rows"] == "92"
    assert fields["Snapshot exists"] == "✓"
    assert fields["Resource matches"] == "✓"
    assert fields["Selector hash matches"] == "✓"
    assert fields["Count within approved maximum"] == "✓"


def test_recovery_proof_card_fields_marks_failed_checks_with_cross():
    proof = RollbackProof(
        snapshot_id="snap-bad",
        resource="users",
        selector_hash="mismatched",
        max_affected_rows=92,
    )
    proof_checks = {
        "snapshot_exists": True,
        "resource_matches": True,
        "selector_hash_matches": False,
        "count_within_approved_maximum": False,
    }
    fields = recovery_proof_card_fields("INVALID", proof, proof_checks)

    assert fields["Validation status"] == "INVALID"
    assert fields["Snapshot exists"] == "✓"
    assert fields["Resource matches"] == "✓"
    assert fields["Selector hash matches"] == "✗"
    assert fields["Count within approved maximum"] == "✗"


def test_recovery_proof_card_fields_returns_only_plain_strings():
    proof = RollbackProof(
        snapshot_id="snap-abc123", resource="users", selector_hash="deadbeef", max_affected_rows=92
    )
    fields = recovery_proof_card_fields("VALID", proof, {"snapshot_exists": True})
    assert all(isinstance(v, str) for v in fields.values())


# ---------------------------------------------------------------------------
# Slice 18: deactivate_users second-tool demonstration presentation helpers
# ---------------------------------------------------------------------------


def test_mutation_verb_label_hard_delete_true_is_deleted():
    assert mutation_verb_label(True) == "deleted"


def test_mutation_verb_label_hard_delete_false_is_deactivated():
    assert mutation_verb_label(False) == "deactivated"


def test_recovery_proof_requirement_label_reversible_missing_is_not_required():
    assert (
        recovery_proof_requirement_label(hard_delete=False, proof_status="MISSING")
        == "Not required for this reversible action"
    )


def test_recovery_proof_requirement_label_hard_delete_falls_back_to_raw_status():
    # hard_delete=True is the existing delete_users case -- this helper is
    # never even consulted for it in app.py, but must not silently rewrite
    # the raw status if it ever were.
    assert recovery_proof_requirement_label(hard_delete=True, proof_status="MISSING") == "MISSING"
    assert recovery_proof_requirement_label(hard_delete=True, proof_status="VALID") == "VALID"


def test_recovery_proof_requirement_label_reversible_non_missing_falls_back_to_raw_status():
    # This fixed demo never actually produces VALID/INVALID for a
    # reversible action (no proof is ever supplied to it), but the helper
    # must not silently invent a "not required" label for statuses it
    # doesn't specifically know how to interpret.
    assert recovery_proof_requirement_label(hard_delete=False, proof_status="VALID") == "VALID"
    assert recovery_proof_requirement_label(hard_delete=False, proof_status="INVALID") == "INVALID"


def test_policy_rule_comparison_rows_matches_the_documented_broad_call_outcome():
    rows = policy_rule_comparison_rows(
        delete_triggered_rule_ids={"RULE_INTENT_BOUNDARY", "RULE_RECOVERY_PROOF", "RULE_WORKFLOW_BUDGET"},
        deactivate_triggered_rule_ids={"RULE_INTENT_BOUNDARY", "RULE_WORKFLOW_BUDGET"},
        delete_verdict="BLOCK",
        deactivate_verdict="BLOCK",
    )
    assert rows == [
        {
            "Policy rule": "RULE_INTENT_BOUNDARY",
            "Delete users": "Triggered",
            "Deactivate users": "Triggered",
        },
        {
            "Policy rule": "RULE_RECOVERY_PROOF",
            "Delete users": "Triggered",
            "Deactivate users": "Not triggered",
        },
        {
            "Policy rule": "RULE_WORKFLOW_BUDGET",
            "Delete users": "Triggered",
            "Deactivate users": "Triggered",
        },
        {"Policy rule": "Verdict", "Delete users": "BLOCK", "Deactivate users": "BLOCK"},
    ]


def test_policy_rule_comparison_rows_handles_no_triggered_rules():
    rows = policy_rule_comparison_rows(
        delete_triggered_rule_ids=set(),
        deactivate_triggered_rule_ids=set(),
        delete_verdict="ALLOW",
        deactivate_verdict="ALLOW",
    )
    for row in rows[:-1]:
        assert row["Delete users"] == "Not triggered"
        assert row["Deactivate users"] == "Not triggered"
    assert rows[-1] == {"Policy rule": "Verdict", "Delete users": "ALLOW", "Deactivate users": "ALLOW"}


def test_comparison_markdown_table_builds_a_well_formed_markdown_table():
    rows = [
        {"Policy rule": "RULE_INTENT_BOUNDARY", "Delete users": "Triggered", "Deactivate users": "Triggered"},
        {"Policy rule": "Verdict", "Delete users": "BLOCK", "Deactivate users": "BLOCK"},
    ]
    table = comparison_markdown_table(rows)
    lines = table.splitlines()
    assert lines[0] == "| Policy rule | Delete users | Deactivate users |"
    assert lines[1] == "| --- | --- | --- |"
    assert lines[2] == "| RULE_INTENT_BOUNDARY | Triggered | Triggered |"
    assert lines[3] == "| Verdict | BLOCK | BLOCK |"


def test_comparison_markdown_table_empty_rows_returns_empty_string():
    assert comparison_markdown_table([]) == ""


# ---------------------------------------------------------------------------
# reset_deactivate_demo_state
# ---------------------------------------------------------------------------


def _deactivate_action_context() -> ActionContext:
    return ActionContext(
        workflow_id=DEACTIVATE_WORKFLOW_ID,
        requesting_user="tester",
        agent_id="tester-agent",
        original_instruction="Deactivate inactive test accounts that have not logged in for 90 days.",
    )


def test_reset_deactivate_demo_state_resets_only_its_own_workflow_budget(monkeypatch, tmp_path):
    import proofgate.audit as audit_module

    monkeypatch.setattr(audit_module, "DEFAULT_AUDIT_PATH", tmp_path / "audit.jsonl")

    guarded_execute(
        tool_name="deactivate_users",
        action_context=_deactivate_action_context(),
        arguments={"inactive_days": 90, "environment": "test"},
        rollback_proof=None,
    )
    assert get_workflow_budget(DEACTIVATE_WORKFLOW_ID).rows_mutated == 92

    reset_deactivate_demo_state(DEACTIVATE_WORKFLOW_ID)

    assert get_workflow_budget(DEACTIVATE_WORKFLOW_ID).rows_mutated == 0


# ---------------------------------------------------------------------------
# Workflow-budget isolation between the two demonstrations (real backend,
# no fabricated/locally-computed values)
# ---------------------------------------------------------------------------


def test_delete_and_deactivate_corrected_workflows_are_isolated_and_both_report_92(
    monkeypatch, tmp_path
):
    """Both demonstrations share one working database and target the exact
    same selector (inactive_days=90, environment="test") on purpose -- so
    the two corrected (mutating) actions must run deactivate-before-delete,
    not delete-before-deactivate, to both affect exactly 92 rows:

    - deactivate_users only sets status='deactivated' -- the 92 rows still
      exist afterward, still matching delete_users' predicate (which never
      inspects status).
    - delete_users performs a real hard DELETE -- if it ran first, those
      same 92 rows would be physically gone, and deactivate_users' preview
      would find 0 eligible rows instead of 92.

    Verified empirically before writing this test: delete-then-deactivate
    leaves deactivate_users with 0 eligible rows. This is why DEMO.md and
    app.py's second section both document the deactivate-before-delete
    corrected-call order, even though the delete section appears first on
    the page and its own internal click order (reset -> unsafe -> repair)
    is otherwise unchanged.
    """
    import proofgate.audit as audit_module

    monkeypatch.setattr(audit_module, "DEFAULT_AUDIT_PATH", tmp_path / "audit.jsonl")

    reset_demo_state(WORKFLOW_ID)
    reset_deactivate_demo_state(DEACTIVATE_WORKFLOW_ID)

    deactivate_result = guarded_execute(
        tool_name="deactivate_users",
        action_context=_deactivate_action_context(),
        arguments={"inactive_days": 90, "environment": "test"},
        rollback_proof=None,
    )
    assert deactivate_result.verdict == "ALLOW"

    from operations.snapshots import create_snapshot

    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    delete_result = guarded_delete_users(
        action_context=_action_context(),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )
    assert delete_result.verdict == "ALLOW"

    delete_budget = get_workflow_budget(WORKFLOW_ID)
    deactivate_budget = get_workflow_budget(DEACTIVATE_WORKFLOW_ID)

    assert delete_budget.rows_mutated == 92
    assert delete_budget.max_rows == 100
    assert deactivate_budget.rows_mutated == 92
    assert deactivate_budget.max_rows == 100
    # Isolation: neither workflow's budget reflects the other's mutation.
    assert delete_budget.workflow_id != deactivate_budget.workflow_id

    reset_demo_state(WORKFLOW_ID)
    reset_deactivate_demo_state(DEACTIVATE_WORKFLOW_ID)
    assert get_workflow_budget(WORKFLOW_ID).rows_mutated == 0
    assert get_workflow_budget(DEACTIVATE_WORKFLOW_ID).rows_mutated == 0


# ---------------------------------------------------------------------------
# Slice 20: honest per-call integration-source labels
# ---------------------------------------------------------------------------


def test_nebius_source_label_not_yet_called():
    assert nebius_source_label(None) == "Not yet called"


def test_nebius_source_label_live():
    assert nebius_source_label("nebius_live") == "Live"


def test_nebius_source_label_mixed():
    assert nebius_source_label("mixed") == "Mixed (partial live)"


def test_nebius_source_label_fallback():
    assert nebius_source_label("deterministic_fallback") == "Fallback (deterministic)"


def test_nebius_source_label_unknown_value_is_labeled_unknown_not_fabricated_as_live():
    assert nebius_source_label("something_new") == "Unknown"


def test_craft_source_label_not_yet_called():
    assert craft_source_label(None) == "Not yet called"


def test_craft_source_label_live():
    assert craft_source_label("live") == "Live"


def test_craft_source_label_cached_without_error_summary_is_not_configured():
    assert craft_source_label("cached", None) == "Cached (not configured for live)"


def test_craft_source_label_cached_with_error_summary_is_live_attempt_failed():
    assert craft_source_label("cached", "[execute_query] TimeoutError: simulated") == (
        "Fallback (live attempt failed)"
    )


def test_craft_source_label_unavailable():
    assert craft_source_label("unavailable") == "Unavailable"


def test_craft_source_label_never_reports_cached_as_live():
    # Regression guard for the core "never mislabel fallback as live" rule.
    assert craft_source_label("cached", "any failure") != "Live"
    assert craft_source_label("cached", None) != "Live"


# ---------------------------------------------------------------------------
# Slice 20.1: craft_evidence_source_label (mode-aware, lazy-live-fetch)
# ---------------------------------------------------------------------------


def test_craft_evidence_source_label_live_success():
    assert craft_evidence_source_label("live", True, "live", None) == "Live"


def test_craft_evidence_source_label_live_mode_before_request():
    assert craft_evidence_source_label("live", False, "cached", None) == (
        "Cached — live fetch not requested"
    )


def test_craft_evidence_source_label_live_mode_after_requested_no_config():
    assert craft_evidence_source_label("live", True, "cached", None) == (
        "Cached fallback — configuration unavailable"
    )


def test_craft_evidence_source_label_live_mode_after_requested_failure():
    assert craft_evidence_source_label("live", True, "cached", "[execute_query] TimeoutError") == (
        "Cached fallback — live retrieval failed"
    )


def test_craft_evidence_source_label_fallback_mode():
    assert craft_evidence_source_label("fallback", False, "cached", None) == (
        "Cached — fallback mode"
    )
    # live_requested is always False in fallback mode (no button shown),
    # but the label must not depend on that alone -- fallback mode wins.
    assert craft_evidence_source_label("fallback", False, "cached", "some failure") == (
        "Cached fallback — live retrieval failed"
    )


def test_craft_evidence_source_label_reliable_demo_mode():
    assert craft_evidence_source_label("reliable_demo", False, "cached", None) == (
        "Reliable demo evidence"
    )
    assert craft_evidence_source_label("reliable_demo", False, "unavailable", None) == (
        "Reliable demo evidence"
    )


def test_craft_evidence_source_label_unavailable():
    assert craft_evidence_source_label("live", True, "unavailable", None) == "Unavailable"


def test_craft_evidence_source_label_never_mislabels_cached_as_live():
    for runtime_mode in ("live", "fallback", "reliable_demo"):
        for live_requested in (True, False):
            for error_summary in (None, "some failure"):
                assert (
                    craft_evidence_source_label(runtime_mode, live_requested, "cached", error_summary)
                    != "Live"
                )
