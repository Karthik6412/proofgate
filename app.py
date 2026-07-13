"""ProofGate Streamlit demo.

Presentation glue over the existing, already-tested backend. All non-UI
logic lives in app_logic.py; this file only wires Streamlit widgets to
the real proofgate.core / operations.* / craft.* functions -- it never
reimplements policy, deletion, proof, or CRAFT logic.

Slice 15: the page is organized around the actual governance pipeline
(User instruction -> Proposed tool invocation -> CRAFT / Operations /
Nebius / Deterministic policy / Recovery proof -> BLOCK or ALLOW ->
Execution result -> Postcondition -> Audit), with visual weight matching
real authority: the deterministic-policy verdict is the most dominant
element on the page; CRAFT and Nebius are compact/subordinate context;
Operations impact and recovery proof are intermediate-weight authoritative
inputs. This is a presentation-only reorganization -- every value shown
still comes from the same real backend calls as before.
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
    DEACTIVATE_INSTRUCTION,
    DEACTIVATE_WORKFLOW_ID,
    FIXED_INSTRUCTION,
    RESET_SUCCESS_MESSAGE,
    WORKFLOW_ID,
    audit_card_fields,
    comparison_markdown_table,
    craft_evidence_display,
    craft_status_label,
    ensure_craft_cache_seeded,
    find_audit_event_by_id,
    format_count,
    format_executed,
    latest_event_by_verdict,
    mutation_verb_label,
    nebius_status_label,
    operations_db_status_label,
    policy_rule_comparison_rows,
    read_audit_rows,
    recovery_proof_card_fields,
    recovery_proof_requirement_label,
    reset_demo_state,
    reset_deactivate_demo_state,
    triggered_rule_rows,
)
from craft.evidence import prepare_craft_evidence
from operations.database import WORKING_DB_PATH, reset_working_db
from operations.snapshots import create_snapshot
from proofgate.audit import DEFAULT_AUDIT_PATH
from proofgate.core import guarded_delete_users, guarded_execute
from proofgate.models import ActionContext

st.set_page_config(page_title="ProofGate", layout="wide")

_BLOCK_COLOR = "#7f1d1d"
_ALLOW_COLOR = "#14532d"
_VERIFIED_COLOR = "#14532d"


def _verdict_banner_html(role_label: str, value: str, background: str) -> str:
    """One shared banner builder for BLOCK / ALLOW / VERIFIED -- the most
    visually dominant element on the page (solid color, full width, large
    text), always labeled with the real component that produced it."""
    return f"""
    <div style="background-color:{background};padding:1rem 1.25rem;border-radius:0.5rem;margin:0.35rem 0;">
      <div style="color:#fff;opacity:0.75;font-size:0.75rem;font-weight:700;
                  letter-spacing:0.08em;text-transform:uppercase;">{role_label}</div>
      <div style="color:#fff;font-size:2.2rem;font-weight:800;line-height:1.25;">{value}</div>
    </div>
    """


def _action_context() -> ActionContext:
    return ActionContext(
        workflow_id=WORKFLOW_ID,
        requesting_user="demo-judge",
        agent_id="demo-agent",
        original_instruction=FIXED_INSTRUCTION,
    )


def _deactivate_action_context() -> ActionContext:
    return ActionContext(
        workflow_id=DEACTIVATE_WORKFLOW_ID,
        requesting_user="demo-judge",
        agent_id="demo-agent",
        original_instruction=DEACTIVATE_INSTRUCTION,
    )


def _render_governance_and_verdict(result, audit_event: dict | None, rollback_proof) -> None:
    """Shared rendering for one governed invocation (unsafe or corrected).

    Renders, in pipeline order: Nebius semantic signals (subordinate) and
    Operations impact (intermediate) side by side; missing requirements;
    Deterministic policy triggered rules immediately followed by the
    dominant BLOCK/ALLOW banner; the recovery-proof verification card
    (intermediate); and, only on ALLOW, the execution result plus the
    dominant postcondition banner. Used identically for both the unsafe
    and corrected sections -- the only difference is the data passed in.
    """
    impact = (audit_event or {}).get("impact_envelope") or {}
    env_counts = impact.get("environment_counts") or {}
    mutation_result = (audit_event or {}).get("mutation_result") or {}
    postcondition_result = (audit_event or {}).get("postcondition_result") or {}
    budget_after = (audit_event or {}).get("workflow_budget_after") or {}
    proof_status = (audit_event or {}).get("proof_status") or "MISSING"
    proof_checks = (audit_event or {}).get("proof_checks")
    # Read directly from the real ImpactEnvelope this specific call
    # produced -- never hardcoded per tool -- so delete_users (hard_delete
    # True) keeps its exact existing wording below, and deactivate_users
    # (hard_delete False) honestly gets its own reversible-action wording.
    hard_delete = impact.get("hard_delete", True)
    mutation_verb = mutation_verb_label(hard_delete)

    with st.container(border=True):
        info_cols = st.columns([1, 2])

        with info_cols[0]:
            st.caption("NEBIUS · semantic extraction — never decides ALLOW/BLOCK")
            if result.risk_factors:
                st.markdown(" ".join(f"`{factor}`" for factor in result.risk_factors))
            else:
                st.markdown("_No risk factors flagged._")
            st.caption(f"Risk score (explanatory only): {result.risk_score:.1f} / 10")

        with info_cols[1]:
            st.caption("OPERATIONS DB · exact blast-radius calculation — authoritative mutation source")
            metric_cols = st.columns(4)
            metric_cols[0].metric("Executed", format_executed(result.executed))
            metric_cols[1].metric("Total affected", format_count(impact.get("estimated_count")))
            metric_cols[2].metric("Production affected", format_count(env_counts.get("production")))
            metric_cols[3].metric("Test affected", format_count(env_counts.get("test")))

        if result.missing_requirements:
            st.warning("Missing before this call can be allowed: " + "; ".join(result.missing_requirements))

        st.divider()

        st.caption("DETERMINISTIC POLICY · final decision authority")
        rule_rows = triggered_rule_rows(result.triggered_rules)
        if rule_rows:
            for rule in rule_rows:
                st.markdown(f"- **{rule['rule_id']}** — {rule['explanation']}")
        else:
            st.markdown("_No rules triggered._")

        banner_color = _ALLOW_COLOR if result.verdict == "ALLOW" else _BLOCK_COLOR
        st.markdown(
            _verdict_banner_html("Deterministic policy verdict", result.verdict, banner_color),
            unsafe_allow_html=True,
        )

        st.caption("RECOVERY PROOF · recoverability evidence — never grants permission by itself")
        if hard_delete:
            proof_fields = recovery_proof_card_fields(proof_status, rollback_proof, proof_checks)
            proof_cols = st.columns(2)
            with proof_cols[0]:
                st.markdown(f"**Validation status:** {proof_fields['Validation status']}")
                st.markdown(f"**Snapshot ID:** {proof_fields['Snapshot ID']}")
                st.markdown(f"**Protected resource:** {proof_fields['Protected resource']}")
                st.markdown(f"**Maximum affected rows:** {proof_fields['Maximum affected rows']}")
            with proof_cols[1]:
                st.markdown(f"**Snapshot exists:** {proof_fields['Snapshot exists']}")
                st.markdown(f"**Resource matches:** {proof_fields['Resource matches']}")
                st.markdown(f"**Selector hash matches:** {proof_fields['Selector hash matches']}")
                st.markdown(f"**Count within approved maximum:** {proof_fields['Count within approved maximum']}")
        else:
            # Reversible action: no proof was supplied and none was
            # required (RULE_RECOVERY_PROOF never applies to hard_delete
            # False). This is not an error -- the raw proof_status is still
            # shown verbatim, just in a technical expander rather than as
            # a headline "MISSING" that would misleadingly read as a fault.
            st.markdown(
                f"**Recovery proof:** {recovery_proof_requirement_label(hard_delete, proof_status)}"
            )
            with st.expander("Technical details (raw proof status)", expanded=False):
                st.json({"proof_status": proof_status})

        if rollback_proof is not None:
            with st.expander("Technical details (raw proof)", expanded=False):
                st.json(
                    {
                        "snapshot_id": rollback_proof.snapshot_id,
                        "resource": rollback_proof.resource,
                        "selector_hash": rollback_proof.selector_hash,
                        "max_affected_rows": rollback_proof.max_affected_rows,
                    }
                )

        if result.verdict == "BLOCK":
            st.markdown("**Structured repair instructions**")
            if result.suggested_repairs:
                st.json(result.suggested_repairs)
            else:
                st.write("None.")
        else:
            st.divider()
            st.caption("EXECUTION RESULT")
            exec_cols = st.columns(3)
            exec_cols[0].metric(
                f"Test users {mutation_verb}", format_count(mutation_result.get("test_affected"))
            )
            exec_cols[1].metric(
                f"Production users {mutation_verb}",
                format_count(mutation_result.get("production_affected")),
            )
            rows_mutated = budget_after.get("rows_mutated")
            max_rows = budget_after.get("max_rows", 100)
            exec_cols[2].metric("Workflow budget", f"{format_count(rows_mutated)} / {format_count(max_rows)}")

            if postcondition_result.get("status") == "VERIFIED":
                st.markdown(
                    _verdict_banner_html("Postcondition verification", "VERIFIED", _VERIFIED_COLOR),
                    unsafe_allow_html=True,
                )
            else:
                st.warning(f"Postcondition: {postcondition_result.get('status', 'UNKNOWN')}")


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
        "just_reset": False,
        "deactivate_broad_result": None,
        "deactivate_broad_error": None,
        "deactivate_corrected_result": None,
        "deactivate_corrected_error": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


_init_session_state()

if not st.session_state.db_initialized:
    reset_working_db()
    st.session_state.db_initialized = True

# Ensure real, previously-retrieved CRAFT evidence is available for cache
# fallback even on a fresh checkout, without ever running live OAuth.
ensure_craft_cache_seeded()

if st.session_state.craft_outcome is None and st.session_state.craft_error is None:
    try:
        st.session_state.craft_outcome = prepare_craft_evidence(WORKFLOW_ID, FIXED_INSTRUCTION)
    except Exception as exc:  # noqa: BLE001 -- show it, never crash the app
        st.session_state.craft_error = str(exc)

# ---------------------------------------------------------------------------
# Sidebar: reset (interaction contract unchanged)
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### Demo controls")
    if st.button("Reset demo", type="secondary"):
        reset_demo_state(WORKFLOW_ID)
        reset_deactivate_demo_state(DEACTIVATE_WORKFLOW_ID)
        st.session_state.clear()
        st.session_state["just_reset"] = True
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
st.caption(
    "User instruction → Proposed tool invocation → CRAFT + Operations + Nebius → "
    "Deterministic policy → Recovery proof → **BLOCK / ALLOW** → Execution → "
    "Postcondition → Audit"
)

if st.session_state.just_reset:
    st.success(RESET_SUCCESS_MESSAGE)
    st.session_state.just_reset = False

craft_mode = st.session_state.craft_outcome.mode if st.session_state.craft_outcome else None
status_cols = st.columns(5)
status_cols[0].caption(f"**Operations DB:** {operations_db_status_label(WORKING_DB_PATH)}")
status_cols[1].caption("**Deterministic policy:** Active")
status_cols[2].caption("**Audit log:** Active")
status_cols[3].caption(f"**CRAFT evidence:** {craft_status_label(craft_mode)}")
status_cols[4].caption(f"**Nebius extraction:** {nebius_status_label()}")

st.divider()

# ---------------------------------------------------------------------------
# User instruction
# ---------------------------------------------------------------------------

st.markdown("### User instruction")
st.info(f'"{FIXED_INSTRUCTION}"')

# ---------------------------------------------------------------------------
# CRAFT enterprise context -- shared, read-only, subordinate, collapsed
# ---------------------------------------------------------------------------

with st.expander("CRAFT enterprise context — read-only, never decides ALLOW/BLOCK", expanded=False):
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
# Proposed tool invocation (unsafe) -- interaction contract unchanged
# ---------------------------------------------------------------------------

st.markdown("### Proposed tool invocation")
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
    _render_governance_and_verdict(result, audit_event, rollback_proof=None)

st.divider()

# ---------------------------------------------------------------------------
# Corrected tool invocation -- interaction contract unchanged
# ---------------------------------------------------------------------------

st.markdown("### Corrected tool invocation")

if st.session_state.unsafe_result is None:
    st.write("Run the unsafe agent action first.")
else:
    st.info(
        'Same public tool `delete_users` — this call adds `environment="test"` '
        "and a verified rollback proof."
    )
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
        _render_governance_and_verdict(
            corrected, audit_event, rollback_proof=st.session_state.rollback_proof
        )

st.divider()

# ---------------------------------------------------------------------------
# Same engine, a different tool -- deactivate_users second-tool
# demonstration (Slice 18). Same shared guarded_execute pipeline as
# delete_users above, called with a different registered tool_name, to
# make ProofGate's genericity visible without opening any expander. This
# section is fixed and deliberate: exactly two tools, no generic picker.
# ---------------------------------------------------------------------------

st.markdown("## Same engine, a different tool")
st.caption(
    "ProofGate evaluates both actions through the same guarded pipeline. "
    "The reversible action does not require snapshot proof."
)
st.caption(
    "This demonstration uses its own workflow budget "
    f"(`{DEACTIVATE_WORKFLOW_ID}`), separate from the delete demonstration's "
    f"workflow budget (`{WORKFLOW_ID}`), so the two corrected mutations can "
    "be compared independently instead of sharing one combined budget."
)

if st.session_state.corrected_result is not None:
    st.warning(
        'The delete demonstration\'s "Apply verified repair" above has already run '
        "and permanently removed the 92 test rows both demonstrations select from "
        "the one shared database. This section's counts will now show 0 affected "
        'test rows. Click "Reset demo" and complete this entire section (both '
        "buttons below) *before* applying the delete demonstration's verified "
        "repair, to see both demonstrations at 92."
    )
else:
    st.caption(
        "Both demonstrations select the same 92 test rows from one shared "
        "database. Complete this entire section (both buttons below) before "
        "applying the delete demonstration's verified repair above, so both "
        "corrected actions affect 92 rows independently."
    )

st.markdown("### Proposed tool invocation (reversible)")
st.code("deactivate_users(inactive_days=90)", language="python")

if st.button("Run reversible broad call", type="primary"):
    if (
        st.session_state.deactivate_broad_result is None
        and st.session_state.deactivate_broad_error is None
    ):
        try:
            st.session_state.deactivate_broad_result = guarded_execute(
                tool_name="deactivate_users",
                action_context=_deactivate_action_context(),
                arguments={"inactive_days": 90, "environment": None},
                rollback_proof=None,
            )
        except Exception as exc:  # noqa: BLE001
            st.session_state.deactivate_broad_error = str(exc)

if st.session_state.deactivate_broad_error:
    st.error(
        f"Backend error while evaluating the reversible broad call: "
        f"{st.session_state.deactivate_broad_error}"
    )

if st.session_state.deactivate_broad_result is not None:
    deactivate_broad = st.session_state.deactivate_broad_result
    deactivate_broad_audit = find_audit_event_by_id(
        DEFAULT_AUDIT_PATH, deactivate_broad.audit_event_id
    )
    _render_governance_and_verdict(deactivate_broad, deactivate_broad_audit, rollback_proof=None)

    st.markdown("#### Same rules, same guarded pipeline — different recovery requirement")
    if st.session_state.unsafe_result is not None:
        comparison_rows = policy_rule_comparison_rows(
            delete_triggered_rule_ids={
                rule.rule_id for rule in st.session_state.unsafe_result.triggered_rules
            },
            deactivate_triggered_rule_ids={
                rule.rule_id for rule in deactivate_broad.triggered_rules
            },
            delete_verdict=st.session_state.unsafe_result.verdict,
            deactivate_verdict=deactivate_broad.verdict,
        )
        st.markdown(comparison_markdown_table(comparison_rows))
        st.caption(
            "Same rules, same guarded pipeline—different recovery requirement "
            "because deactivation is reversible."
        )
    else:
        st.caption("Run the unsafe delete action above to see the full side-by-side comparison.")

st.divider()

st.markdown("### Corrected tool invocation (reversible)")

if st.session_state.deactivate_broad_result is None:
    st.write("Run the reversible broad call first.")
else:
    st.info(
        'Same public tool `deactivate_users` — this call adds `environment="test"`. '
        "No rollback proof is required because this action is reversible."
    )
    st.code(
        'deactivate_users(\n'
        f'    inactive_days={CORRECTED_INACTIVE_DAYS},\n'
        '    environment="test",\n'
        ')',
        language="python",
    )

    if st.button("Apply reversible repair", type="primary"):
        if (
            st.session_state.deactivate_corrected_result is None
            and st.session_state.deactivate_corrected_error is None
        ):
            try:
                st.session_state.deactivate_corrected_result = guarded_execute(
                    tool_name="deactivate_users",
                    action_context=_deactivate_action_context(),
                    arguments={"inactive_days": CORRECTED_INACTIVE_DAYS, "environment": "test"},
                    rollback_proof=None,
                )
            except Exception as exc:  # noqa: BLE001
                st.session_state.deactivate_corrected_error = str(exc)

    if st.session_state.deactivate_corrected_error:
        st.error(
            f"Backend error while applying the reversible repair: "
            f"{st.session_state.deactivate_corrected_error}"
        )

    if st.session_state.deactivate_corrected_result is not None:
        deactivate_corrected = st.session_state.deactivate_corrected_result
        deactivate_corrected_audit = find_audit_event_by_id(
            DEFAULT_AUDIT_PATH, deactivate_corrected.audit_event_id
        )
        _render_governance_and_verdict(
            deactivate_corrected, deactivate_corrected_audit, rollback_proof=None
        )

with st.expander("Deactivate audit trail", expanded=False):
    deactivate_rows = read_audit_rows(DEFAULT_AUDIT_PATH, workflow_id=DEACTIVATE_WORKFLOW_ID)
    if not deactivate_rows:
        st.write("No audit events recorded yet.")
    else:
        deactivate_block_row = latest_event_by_verdict(deactivate_rows, "BLOCK")
        deactivate_allow_row = latest_event_by_verdict(deactivate_rows, "ALLOW")

        deactivate_audit_cols = st.columns(2)
        with deactivate_audit_cols[0]:
            st.markdown("**BLOCK event**")
            st.json(audit_card_fields(deactivate_block_row))
        with deactivate_audit_cols[1]:
            st.markdown("**ALLOW event**")
            st.json(audit_card_fields(deactivate_allow_row))

st.divider()

# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

with st.expander("Audit trail", expanded=False):
    rows = read_audit_rows(DEFAULT_AUDIT_PATH, workflow_id=WORKFLOW_ID)
    if not rows:
        st.write("No audit events recorded yet.")
    else:
        block_row = latest_event_by_verdict(rows, "BLOCK")
        allow_row = latest_event_by_verdict(rows, "ALLOW")

        audit_cols = st.columns(2)
        with audit_cols[0]:
            st.markdown("**BLOCK event**")
            st.json(audit_card_fields(block_row))
        with audit_cols[1]:
            st.markdown("**ALLOW event**")
            st.json(audit_card_fields(allow_row))
