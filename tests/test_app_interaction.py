"""End-to-end Streamlit interaction test for Slice 18 using
streamlit.testing.v1.AppTest -- clicks through both the existing
delete_users demonstration and the new deactivate_users demonstration
against the real backend (no mocks), then triggers reset and confirms a
clean return to the initial state.

Click order note: both demonstrations deliberately select the exact same
92 test rows from the one shared operations/working.db (same
inactive_days=90/environment="test" selector, to prove genericity with
identical counts). delete_users performs a real hard DELETE; deactivate_
users only sets status='deactivated' and delete_users' predicate never
inspects status. So this test (and DEMO.md) apply the reversible repair
BEFORE the verified repair -- the reverse order would leave 0 rows for
deactivate_users to affect, since delete_users would have already removed
them. This was verified directly against operations.actions before this
test was written. Each section's own internal click order (broad/unsafe
-> corrected/repair) is otherwise exactly as specified and unchanged.

Runs against the real operations/working.db, like the rest of this
repo's integration-style tests (see test_app_logic.py's
reset_demo_state tests) -- not a fully isolated per-test database.
"""

import re

import pytest

from streamlit.testing.v1 import AppTest

pytestmark = pytest.mark.usefixtures(
    "_isolate_audit_log",
    "_disable_live_nebius_by_default",
    "_isolate_craft_by_default",
    "_force_fallback_runtime_mode",
)


def _click(at: AppTest, label: str) -> None:
    for button in at.button:
        if button.label == label:
            button.click().run()
            return
    raise AssertionError(f"No button labeled {label!r} found. Buttons: {[b.label for b in at.button]}")


def _verdict_values(at: AppTest, role_label: str) -> list[str]:
    """All rendered verdict/postcondition banner values whose role_label
    caption matches (in page order) -- e.g. every "Deterministic policy
    verdict" banner's big BLOCK/ALLOW text, across both demonstrations."""
    values = []
    for md in at.markdown:
        if role_label in md.value:
            match = re.search(r'font-weight:800;line-height:1\.25;">([^<]+)</div>', md.value)
            if match:
                values.append(match.group(1))
    return values


def _metric_value(at: AppTest, label: str) -> str | None:
    matches = [m.value for m in at.metric if m.label == label]
    return matches[-1] if matches else None


def _all_metric_values(at: AppTest, label: str) -> list[str]:
    return [m.value for m in at.metric if m.label == label]


def _rule_bullet_count(at: AppTest, rule_id: str) -> int:
    return sum(1 for md in at.markdown if md.value.startswith(f"- **{rule_id}**"))


@pytest.fixture(autouse=True)
def _reset_real_backend():
    from operations.database import reset_working_db
    from proofgate.budgets import reset_workflow_state

    reset_working_db()
    reset_workflow_state("demo-workflow")
    reset_workflow_state("deactivate-demo-workflow")
    yield
    reset_working_db()
    reset_workflow_state("demo-workflow")
    reset_workflow_state("deactivate-demo-workflow")


def test_full_two_tool_click_through_and_reset():
    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()
    assert not at.exception

    # ---- 1. Initial state: only the two "start" buttons and reset exist ----
    initial_labels = {b.label for b in at.button}
    assert initial_labels == {"Run unsafe agent action", "Run reversible broad call", "Reset demo"}

    # ---- 2/3. Run the existing broad delete call ----
    _click(at, "Run unsafe agent action")
    assert not at.exception
    assert _metric_value(at, "Total affected") == "10,073"
    assert _metric_value(at, "Production affected") == "9,981"
    assert _metric_value(at, "Test affected") == "92"
    assert _metric_value(at, "Executed") == "No"
    assert _rule_bullet_count(at, "RULE_INTENT_BOUNDARY") == 1
    assert _rule_bullet_count(at, "RULE_RECOVERY_PROOF") == 1
    assert _rule_bullet_count(at, "RULE_WORKFLOW_BUDGET") == 1
    assert _verdict_values(at, "Deterministic policy verdict") == ["BLOCK"]

    # ---- 6/7. Run the new broad deactivate call (before applying any
    # repair, so its preview still sees the un-mutated 92 test rows) ----
    _click(at, "Run reversible broad call")
    assert not at.exception
    # Now two BLOCK banners exist: delete's (still BLOCK) and deactivate's.
    assert _verdict_values(at, "Deterministic policy verdict") == ["BLOCK", "BLOCK"]
    assert _all_metric_values(at, "Total affected") == ["10,073", "10,073"]
    assert _all_metric_values(at, "Production affected") == ["9,981", "9,981"]
    assert _all_metric_values(at, "Test affected") == ["92", "92"]
    # RULE_RECOVERY_PROOF only ever triggers for the hard-delete tool.
    assert _rule_bullet_count(at, "RULE_RECOVERY_PROOF") == 1
    assert _rule_bullet_count(at, "RULE_INTENT_BOUNDARY") == 2
    assert _rule_bullet_count(at, "RULE_WORKFLOW_BUDGET") == 2
    comparison_markdown = "\n".join(md.value for md in at.markdown)
    assert "Not triggered" in comparison_markdown
    assert "RULE_RECOVERY_PROOF" in comparison_markdown

    # ---- 8/9. Apply the reversible repair FIRST (see module docstring for
    # why this must happen before the delete repair) ----
    _click(at, "Apply reversible repair")
    assert not at.exception
    assert _verdict_values(at, "Deterministic policy verdict")[-1] == "ALLOW"
    assert _verdict_values(at, "Postcondition verification") == ["VERIFIED"]
    assert _metric_value(at, "Test users deactivated") == "92"
    assert _metric_value(at, "Production users deactivated") == "0"
    assert _metric_value(at, "Workflow budget") == "92 / 100"
    assert any(
        "Not required for this reversible action" in md.value for md in at.markdown
    )
    assert not any("Technical details (raw proof)" == e.label for e in at.expander)

    # ---- 4/5. Now apply the existing delete repair ----
    _click(at, "Apply verified repair")
    assert not at.exception
    # Final page order top-to-bottom: delete-broad, delete-corrected,
    # deactivate-broad, deactivate-corrected.
    assert _verdict_values(at, "Deterministic policy verdict") == [
        "BLOCK",
        "ALLOW",
        "BLOCK",
        "ALLOW",
    ]
    assert _verdict_values(at, "Postcondition verification") == ["VERIFIED", "VERIFIED"]
    assert _metric_value(at, "Test users deleted") == "92"
    assert _metric_value(at, "Production users deleted") == "0"
    assert _all_metric_values(at, "Workflow budget") == ["92 / 100", "92 / 100"]

    from operations.actions import count_rows
    from operations.database import WORKING_DB_PATH
    from proofgate.budgets import get_workflow_budget

    assert count_rows(WORKING_DB_PATH) == 10531  # 10623 - 92 deactivated-then-deleted
    assert get_workflow_budget("demo-workflow").rows_mutated == 92
    assert get_workflow_budget("deactivate-demo-workflow").rows_mutated == 92

    # ---- 10/11. Reset and confirm a clean return to initial state ----
    _click(at, "Reset demo")
    assert not at.exception
    reset_labels = {b.label for b in at.button}
    assert reset_labels == {"Run unsafe agent action", "Run reversible broad call", "Reset demo"}
    assert get_workflow_budget("demo-workflow").rows_mutated == 0
    assert get_workflow_budget("deactivate-demo-workflow").rows_mutated == 0
    assert count_rows(WORKING_DB_PATH) == 10623


# ---------------------------------------------------------------------------
# Slice 20: runtime-mode visibility
# ---------------------------------------------------------------------------


def test_runtime_mode_and_integration_source_are_visible(monkeypatch):
    from operations.database import reset_working_db
    from proofgate.budgets import reset_workflow_state

    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "fallback")
    reset_working_db()
    reset_workflow_state("demo-workflow")
    reset_workflow_state("deactivate-demo-workflow")

    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()
    assert not at.exception

    captions_before = "\n".join(c.value for c in at.caption)
    assert "Runtime mode" in captions_before
    assert "Fallback" in captions_before

    _click(at, "Run unsafe agent action")
    assert not at.exception
    captions_after = "\n".join(c.value for c in at.caption)
    assert "Source for this call" in captions_after
    assert "Fallback (deterministic)" in captions_after
    # Never mislabel deterministic fallback output as live.
    assert "Source for this call: Live" not in captions_after

    reset_working_db()
    reset_workflow_state("demo-workflow")
    reset_workflow_state("deactivate-demo-workflow")
