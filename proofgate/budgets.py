"""Per-workflow ProofGate runtime state: budget, last MutationResult, last
PostconditionResult, and stored CraftEvidence, keyed by workflow_id.

This is ProofGate's own in-process state and is entirely separate from
the database. operations.reset_working_db resets only the database and
never touches this store; reset_workflow_state resets only this store
and never touches the database. The future orchestrator/UI calls both
explicitly. Operations must never import or invoke the CRAFT client --
this module only stores/retrieves whatever CraftEvidence craft.evidence
already prepared; it never fetches evidence itself.
"""

from dataclasses import dataclass

from proofgate.models import CraftEvidence, MutationResult, PostconditionResult, WorkflowBudget


@dataclass
class WorkflowState:
    budget: WorkflowBudget
    last_mutation_result: MutationResult | None = None
    last_postcondition_result: PostconditionResult | None = None
    craft_evidence: CraftEvidence | None = None


_workflow_states: dict[str, WorkflowState] = {}


def _get_or_create_state(workflow_id: str) -> WorkflowState:
    if workflow_id not in _workflow_states:
        _workflow_states[workflow_id] = WorkflowState(
            budget=WorkflowBudget(workflow_id=workflow_id)
        )
    return _workflow_states[workflow_id]


def get_workflow_budget(workflow_id: str) -> WorkflowBudget:
    return _get_or_create_state(workflow_id).budget


def get_last_mutation_result(workflow_id: str) -> MutationResult | None:
    return _get_or_create_state(workflow_id).last_mutation_result


def get_last_postcondition_result(workflow_id: str) -> PostconditionResult | None:
    return _get_or_create_state(workflow_id).last_postcondition_result


def record_execution(
    workflow_id: str,
    mutation_result: MutationResult,
    postcondition_result: PostconditionResult,
) -> None:
    """Store both results and update the budget using actual mutation
    counts. Called only after a genuine ALLOW execution; budget accounting
    uses affected_count regardless of postcondition status."""
    state = _get_or_create_state(workflow_id)
    state.last_mutation_result = mutation_result
    state.last_postcondition_result = postcondition_result
    state.budget.rows_mutated += mutation_result.affected_count
    state.budget.production_rows_mutated += mutation_result.production_affected


def get_craft_evidence(workflow_id: str) -> CraftEvidence | None:
    return _get_or_create_state(workflow_id).craft_evidence


def record_craft_evidence(workflow_id: str, evidence: CraftEvidence | None) -> None:
    _get_or_create_state(workflow_id).craft_evidence = evidence


def reset_workflow_state(workflow_id: str) -> None:
    """Reset only ProofGate's in-process state for this workflow_id
    (budget, mutation result, postcondition result, and CRAFT evidence).
    Never touches the database."""
    _workflow_states.pop(workflow_id, None)
