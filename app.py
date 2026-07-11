"""ProofGate Streamlit demo.

Presentation glue over the existing, already-tested backend. All non-UI
logic lives in app_logic.py; this file only wires Streamlit widgets to
the real proofgate.core / operations.* / craft.* functions -- it never
reimplements policy, deletion, proof, or CRAFT logic.
"""

import os

# This slice must not run the live CRAFT OAuth flow -- force cached-only
# CRAFT evidence regardless of the .env configuration. A later slice can
# remove this to enable the real live demo path.
os.environ["CRAFT_LIVE_ENABLED"] = "false"

import streamlit as st

from app_logic import (
    CORRECTED_INACTIVE_DAYS,
    CORRECTED_MAX_AFFECTED_ROWS,
    FIXED_INSTRUCTION,
    WORKFLOW_ID,
    craft_evidence_display,
    find_audit_event_by_id,
    format_count,
    format_executed,
    read_audit_rows,
    reset_demo_state,
    triggered_rule_rows,
)
from craft.evidence import prepare_craft_evidence
from operations.database import reset_working_db
from operations.snapshots import create_snapshot
from proofgate.audit import DEFAULT_AUDIT_PATH
from proofgate.core import guarded_delete_users
from proofgate.models import ActionContext

st.set_page_config(page_title="ProofGate", layout="wide")

_BLOCK_BANNER = """
<div style="background-color:#7f1d1d;padding:0.75rem 1rem;border-radius:0.5rem;">
  <span style="color:#fff;font-size:2rem;font-weight:800;">VERDICT: BLOCK</span>
</div>
"""
_ALLOW_BANNER = """
<div style="background-color:#14532d;padding:0.75rem 1rem;border-radius:0.5rem;">
  <span style="color:#fff;font-size:2rem;font-weight:800;">VERDICT: ALLOW</span>
</div>
"""
_VERIFIED_BANNER = """
<div style="background-color:#14532d;padding:0.5rem 1rem;border-radius:0.5rem;margin-top:0.5rem;">
  <span style="color:#fff;font-size:1.4rem;font-weight:700;">POSTCONDITION: VERIFIED</span>
</div>
"""


def _action_context() -> ActionContext:
    return ActionContext(
        workflow_id=WORKFLOW_ID,
        requesting_user="demo-judge",
        agent_id="demo-agent",
        original_instruction=FIXED_INSTRUCTION,
    )


def _init_session_state() -> None:
    defaults = {
        "db_initialized": False,
        "unsafe_result": None,
        "unsafe_error": None,
        "corrected_result": None,
        "corrected_error": None,
        "rollback_proof": None,
        "craft_outcome": None,
        "craft_error": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


_init_session_state()

if not st.session_state.db_initialized:
    reset_working_db()
    st.session_state.db_initialized = True

if st.session_state.craft_outcome is None and st.session_state.craft_error is None:
    try:
        st.session_state.craft_outcome = prepare_craft_evidence(WORKFLOW_ID, FIXED_INSTRUCTION)
    except Exception as exc:  # noqa: BLE001 -- show it, never crash the app
        st.session_state.craft_error = str(exc)

# ---------------------------------------------------------------------------
# Sidebar: reset
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### Demo controls")
    if st.button("Reset demo", type="secondary"):
        reset_demo_state(WORKFLOW_ID)
        st.session_state.clear()
        st.rerun()

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("ProofGate")
st.markdown("#### Dangerous actions need proof, not promises.")
st.write(
    "ProofGate intercepts consequential AI-agent tool calls, calculates their "
    "blast radius, and enforces deterministic safety policy before execution."
)

st.divider()

# ---------------------------------------------------------------------------
# Agent intent
# ---------------------------------------------------------------------------

st.markdown("### Agent instruction")
st.info(f'"{FIXED_INSTRUCTION}"')

st.divider()

# ---------------------------------------------------------------------------
# Unsafe action
# ---------------------------------------------------------------------------

st.markdown("### Unsafe agent action")
st.code("delete_users(inactive_days=90)", language="python")

if st.button("Run unsafe agent action", type="primary"):
    if st.session_state.unsafe_result is None and st.session_state.unsafe_error is None:
        try:
            st.session_state.unsafe_result = guarded_delete_users(
                action_context=_action_context(),
                inactive_days=90,
                environment=None,
                rollback_proof=None,
            )
        except Exception as exc:  # noqa: BLE001
            st.session_state.unsafe_error = str(exc)

if st.session_state.unsafe_error:
    st.error(f"Backend error while evaluating the unsafe action: {st.session_state.unsafe_error}")

if st.session_state.unsafe_result is not None:
    result = st.session_state.unsafe_result
    audit_event = find_audit_event_by_id(DEFAULT_AUDIT_PATH, result.audit_event_id)
    impact = (audit_event or {}).get("impact_envelope") or {}
    env_counts = impact.get("environment_counts") or {}

    if result.verdict == "BLOCK":
        st.markdown(_BLOCK_BANNER, unsafe_allow_html=True)
    else:
        st.markdown(_ALLOW_BANNER, unsafe_allow_html=True)

    cols = st.columns(4)
    cols[0].metric("Executed", format_executed(result.executed))
    cols[1].metric("Total affected", format_count(impact.get("estimated_count")))
    cols[2].metric("Production affected", format_count(env_counts.get("production")))
    cols[3].metric("Test affected", format_count(env_counts.get("test")))

    st.metric("Risk score", f"{result.risk_score:.1f} / 10")

    st.markdown("**Triggered deterministic rules**")
    rule_rows = triggered_rule_rows(result.triggered_rules)
    if rule_rows:
        st.table(rule_rows)
    else:
        st.write("None.")

    st.markdown("**Structured repair instructions**")
    if result.suggested_repairs:
        st.json(result.suggested_repairs)
    else:
        st.write("None.")

st.divider()

# ---------------------------------------------------------------------------
# Corrected action
# ---------------------------------------------------------------------------

st.markdown("### Corrected action")

if st.session_state.unsafe_result is None:
    st.write("Run the unsafe agent action first.")
else:
    st.code(
        'delete_users(\n'
        f'    inactive_days={CORRECTED_INACTIVE_DAYS},\n'
        '    environment="test",\n'
        '    rollback_proof=<verified snapshot proof>,\n'
        ')',
        language="python",
    )

    if st.button("Apply verified repair", type="primary"):
        if st.session_state.corrected_result is None and st.session_state.corrected_error is None:
            try:
                proof = create_snapshot(
                    resource="users",
                    inactive_days=CORRECTED_INACTIVE_DAYS,
                    environment="test",
                    max_affected_rows=CORRECTED_MAX_AFFECTED_ROWS,
                )
                st.session_state.rollback_proof = proof
                st.session_state.corrected_result = guarded_delete_users(
                    action_context=_action_context(),
                    inactive_days=CORRECTED_INACTIVE_DAYS,
                    environment="test",
                    rollback_proof=proof,
                )
            except Exception as exc:  # noqa: BLE001
                st.session_state.corrected_error = str(exc)

    if st.session_state.corrected_error:
        st.error(
            f"Backend error while applying the corrected action: {st.session_state.corrected_error}"
        )

    if st.session_state.corrected_result is not None:
        corrected = st.session_state.corrected_result
        audit_event = find_audit_event_by_id(DEFAULT_AUDIT_PATH, corrected.audit_event_id)
        mutation_result = (audit_event or {}).get("mutation_result") or {}
        postcondition_result = (audit_event or {}).get("postcondition_result") or {}
        budget_after = (audit_event or {}).get("workflow_budget_after") or {}

        if corrected.verdict == "ALLOW":
            st.markdown(_ALLOW_BANNER, unsafe_allow_html=True)
        else:
            st.markdown(_BLOCK_BANNER, unsafe_allow_html=True)

        cols = st.columns(4)
        cols[0].metric("Executed", format_executed(corrected.executed))
        cols[1].metric("Test users deleted", format_count(mutation_result.get("test_affected")))
        cols[2].metric(
            "Production users deleted", format_count(mutation_result.get("production_affected"))
        )
        rows_mutated = budget_after.get("rows_mutated")
        max_rows = budget_after.get("max_rows", 100)
        cols[3].metric(
            "Workflow budget",
            f"{format_count(rows_mutated)} / {format_count(max_rows)}",
        )

        if postcondition_result.get("status") == "VERIFIED":
            st.markdown(_VERIFIED_BANNER, unsafe_allow_html=True)
        else:
            st.warning(f"Postcondition: {postcondition_result.get('status', 'UNKNOWN')}")

        proof = st.session_state.rollback_proof
        if proof is not None:
            st.markdown("**Rollback proof (snapshot)**")
            st.json(
                {
                    "snapshot_id": proof.snapshot_id,
                    "resource": proof.resource,
                    "selector_hash": proof.selector_hash,
                    "max_affected_rows": proof.max_affected_rows,
                }
            )

st.divider()

# ---------------------------------------------------------------------------
# CRAFT evidence panel
# ---------------------------------------------------------------------------

with st.expander("CRAFT enterprise evidence", expanded=False):
    if st.session_state.craft_error:
        st.error(f"CRAFT evidence unavailable: {st.session_state.craft_error}")
    else:
        outcome = st.session_state.craft_outcome
        evidence = craft_evidence_display(outcome.evidence if outcome else None)
        if evidence is None:
            st.write("No CRAFT evidence is currently available (no live result and no cache).")
        else:
            st.caption("Read-only enterprise context — not authoritative for mutation impact")
            st.write(f"**Label:** {evidence['label']}")
            st.write(f"**Mode:** {evidence['mode']}")
            st.write(f"**Question:** {evidence['question']}")
            st.markdown("**Generated SQL**")
            st.code(evidence["generated_sql"], language="sql")
            st.write(f"**Result summary:** {evidence['result_summary']}")
            if evidence["show_result_preview"]:
                st.markdown("**Result preview**")
                st.json(evidence["result_preview"])
            st.write(f"**Tool trace:** {' → '.join(evidence['tool_trace'])}")
            if evidence["retrieved_at"]:
                st.caption(f"Retrieved at: {evidence['retrieved_at']}")

st.divider()

# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

with st.expander("Audit trail", expanded=False):
    rows = read_audit_rows(DEFAULT_AUDIT_PATH, workflow_id=WORKFLOW_ID)
    if not rows:
        st.write("No audit events recorded yet.")
    else:
        st.dataframe(rows, use_container_width=True)
