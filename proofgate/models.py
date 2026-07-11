"""Pydantic contracts. Only models needed by the current slice are defined."""

from typing import Literal

from pydantic import BaseModel


class ImpactEnvelope(BaseModel):
    tool_name: str
    estimated_count: int
    environment_counts: dict[str, int]
    hard_delete: bool
    reversibility: str
    selector_hash: str
    generated_at: str


class MutationResult(BaseModel):
    affected_count: int
    production_affected: int
    test_affected: int


class ActionContext(BaseModel):
    workflow_id: str
    requesting_user: str
    agent_id: str
    original_instruction: str


class IntentConstraints(BaseModel):
    action_type: str
    target_resource: str
    environment: str | None
    inactivity_days: int | None
    confidence: float


class RiskFeatures(BaseModel):
    intent_mismatch: bool
    missing_constraints: list[str]
    scope_class: str
    production_impact: bool
    irreversible: bool
    uncertainty: float
    rationale: list[str]


class TriggeredRule(BaseModel):
    rule_id: str
    explanation: str


class WorkflowBudget(BaseModel):
    workflow_id: str
    rows_mutated: int = 0
    production_rows_mutated: int = 0
    max_rows: int = 100


class RollbackProof(BaseModel):
    snapshot_id: str
    resource: str
    selector_hash: str
    max_affected_rows: int


class ProofValidationResult(BaseModel):
    snapshot_exists: bool
    resource_matches: bool
    selector_hash_matches: bool
    count_within_approved_maximum: bool
    valid: bool


class PostconditionResult(BaseModel):
    predicted_count: int
    actual_count: int
    production_affected: int
    status: Literal[
        "VERIFIED",
        "MISMATCH",
        "ROLLED_BACK",
        "MANUAL_REVIEW_REQUIRED",
    ]


class CraftEvidence(BaseModel):
    label: Literal["CRAFT enterprise evidence", "Previously retrieved CRAFT evidence"]
    mode: Literal["live", "cached"]
    database: str
    question: str
    generated_sql: str
    result_summary: str
    result_preview: list[dict]
    tool_trace: list[str]
    retrieved_at: str
    authoritative_for_mutation_impact: Literal[False]


class EnforcementResult(BaseModel):
    verdict: Literal["ALLOW", "BLOCK"]
    risk_score: float
    risk_factors: list[str]
    triggered_rules: list[TriggeredRule]
    missing_requirements: list[str]
    suggested_repairs: list[dict]
    executed: bool
    audit_event_id: str


class AuditEvent(BaseModel):
    schema_version: str
    event_id: str
    workflow_id: str
    timestamp: str
    original_instruction: str
    requesting_user: str
    agent_id: str
    tool_name: str
    tool_arguments: dict
    intent_constraints: IntentConstraints
    impact_envelope: ImpactEnvelope | None
    risk_features: RiskFeatures | None
    risk_score: float
    risk_factors: list[str]
    triggered_rules: list[TriggeredRule]
    policy_version: str
    extraction_mode: str
    nebius_model: str | None
    craft_evidence: CraftEvidence | None
    verdict: Literal["ALLOW", "BLOCK"]
    missing_requirements: list[str]
    suggested_repairs: list[dict]
    proof_status: Literal["MISSING", "INVALID", "VALID"]
    proof_checks: dict[str, bool] | None
    execution_status: Literal["NOT_EXECUTED", "EXECUTED"]
    mutation_result: MutationResult | None
    postcondition_result: PostconditionResult | None
    workflow_budget_before: WorkflowBudget
    workflow_budget_after: WorkflowBudget
