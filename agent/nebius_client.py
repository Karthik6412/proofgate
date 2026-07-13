"""Intent and risk-feature extraction.

extract_intent and extract_risk_features are the deterministic regex
fallback (unchanged since the slice that introduced them -- existing
tests depend on their exact behavior and take no network dependency).

extract_intent_live_or_fallback and extract_risk_features_live_or_fallback
are the additive live-Nebius upgrade: they attempt a real call to the
Nebius Token Factory OpenAI-compatible endpoint, validate the structured
result strictly, and fall back to the deterministic functions above on
ANY failure. No exception ever propagates to the caller. Nebius never
returns ALLOW/BLOCK and is never the security authority -- deterministic
Python policy (proofgate.policy) is the sole source of the verdict, and
it consumes only intent.environment and the authoritative ImpactEnvelope
for RULE_INTENT_BOUNDARY/RULE_WORKFLOW_BUDGET, never RiskFeatures.
"""

import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from proofgate.models import ImpactEnvelope, IntentConstraints, RiskFeatures

logger = logging.getLogger("agent.nebius_client")


def _configure_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_configure_logging()

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

_ENVIRONMENT_PATTERNS = {
    "test": re.compile(r"\btest\b"),
    "production": re.compile(r"\bproduction\b"),
}
_INACTIVITY_PATTERN = re.compile(r"(\d+)\s*day")
_DELETE_ACTION_PATTERN = re.compile(r"delete|clean\s*up|remove|purge")
_DEACTIVATE_ACTION_PATTERN = re.compile(r"deactivat(e|ion)")


def _scope_class_for_count(count: int) -> str:
    """Shared deterministic scope bucketing, used by both the deterministic
    fallback and to override any live-model-supplied scope_class."""
    if count > 1000:
        return "mass"
    if count > 100:
        return "large"
    if count > 10:
        return "moderate"
    return "small"


def extract_intent(instruction: str) -> IntentConstraints:
    """Deterministic regex fallback intent extraction."""
    text = instruction.lower()

    environment = None
    for env_name, pattern in _ENVIRONMENT_PATTERNS.items():
        if pattern.search(text):
            environment = env_name
            break

    inactivity_match = _INACTIVITY_PATTERN.search(text)
    inactivity_days = int(inactivity_match.group(1)) if inactivity_match else None

    if _DEACTIVATE_ACTION_PATTERN.search(text):
        action_type = "deactivate_users"
    elif _DELETE_ACTION_PATTERN.search(text):
        action_type = "delete_users"
    else:
        action_type = "unknown"
    target_resource = "users" if ("user" in text or "account" in text) else "unknown"

    resolved_all = (
        action_type != "unknown"
        and target_resource != "unknown"
        and environment is not None
        and inactivity_days is not None
    )
    confidence = 0.98 if resolved_all else 0.4

    return IntentConstraints(
        action_type=action_type,
        target_resource=target_resource,
        environment=environment,
        inactivity_days=inactivity_days,
        confidence=confidence,
    )


def extract_risk_features(
    intent: IntentConstraints,
    proposed_arguments: dict,
    impact: ImpactEnvelope,
) -> RiskFeatures:
    """Deterministic fallback risk-feature derivation.

    Uses only IntentConstraints, the proposed action arguments, and the
    authoritative ImpactEnvelope. Never invents counts.
    """
    proposed_environment = proposed_arguments.get("environment")

    intent_mismatch = (
        intent.environment is not None and proposed_environment != intent.environment
    )
    missing_constraints = [f"environment={intent.environment}"] if intent_mismatch else []

    production_impact = impact.environment_counts.get("production", 0) > 0

    scope_class = _scope_class_for_count(impact.estimated_count)
    irreversible = impact.hard_delete

    rationale = []
    if intent_mismatch:
        rationale.append("The requested environment constraint is absent.")
    if production_impact:
        rationale.append("The measured action includes production users.")

    return RiskFeatures(
        intent_mismatch=intent_mismatch,
        missing_constraints=missing_constraints,
        scope_class=scope_class,
        production_impact=production_impact,
        irreversible=irreversible,
        uncertainty=0.03,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Live Nebius Token Factory integration (additive upgrade)
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"
DEFAULT_TIMEOUT_SECONDS = 10.0

FORBIDDEN_RESPONSE_KEYS = {"verdict", "decision", "authorization", "allow", "block"}

# Narrow, MVP-scoped synonym acceptance for live-response canonicalization.
# This is not a generic ontology system: unrecognized values are rejected
# outright (causing a deterministic fallback), never guessed at.
_ACTION_TYPE_SYNONYMS = {
    "delete": "delete_users",
    "delete_user": "delete_users",
    "delete_users": "delete_users",
    "deactivate": "deactivate_users",
    "deactivate_user": "deactivate_users",
    "deactivate_users": "deactivate_users",
    "deactivation": "deactivate_users",
}

_TARGET_RESOURCE_SYNONYMS = {
    "user": "users",
    "users": "users",
    "user account": "users",
    "user accounts": "users",
    "account": "users",
    "accounts": "users",
}

_SUPPORTED_INTENT_ENVIRONMENTS = {"test", "production", None}


def _canonicalize_action_type(value: str) -> str:
    canonical = _ACTION_TYPE_SYNONYMS.get(value.strip().lower())
    if canonical is None:
        raise ValueError(f"Unsupported action_type: {value!r}")
    return canonical


def _canonicalize_target_resource(value: str) -> str:
    canonical = _TARGET_RESOURCE_SYNONYMS.get(value.strip().lower())
    if canonical is None:
        raise ValueError(f"Unsupported target_resource: {value!r}")
    return canonical


@dataclass
class ExtractionOutcome:
    """Internal runtime metadata, not a wire contract -- a dataclass is
    fine here. value is always one of the existing Pydantic contracts.

    degraded_reason (Slice 20) honestly distinguishes why mode fell back
    to "deterministic_fallback": no attempt was made because live wasn't
    configured/enabled ("missing_configuration"), a live attempt failed
    to reach or parse a response ("upstream_failure"), or a live response
    was received but failed schema/business validation
    ("invalid_response"). None when mode == "nebius_live", or when the
    caller passed an explicit test client (should_attempt was true for
    reasons other than live configuration)."""

    value: IntentConstraints | RiskFeatures
    mode: Literal["nebius_live", "deterministic_fallback"]
    model: str | None
    degraded_reason: Literal["missing_configuration", "upstream_failure", "invalid_response"] | None = None


def live_enabled() -> bool:
    return os.environ.get("NEBIUS_LIVE_ENABLED", "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def has_api_key() -> bool:
    return _api_key() is not None


def base_url() -> str:
    return os.environ.get("NEBIUS_BASE_URL", DEFAULT_BASE_URL)


def model() -> str:
    return os.environ.get("NEBIUS_MODEL", DEFAULT_MODEL)


def _api_key() -> str | None:
    return os.environ.get("NEBIUS_API_KEY") or None


def _build_client():
    """Construct the real OpenAI-compatible client. Never called when
    live mode is disabled or no API key is configured, and monkeypatched
    directly in tests so no real client is ever constructed there."""
    from openai import OpenAI

    return OpenAI(api_key=_api_key(), base_url=base_url(), timeout=DEFAULT_TIMEOUT_SECONDS)


class _StrictIntentSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: str
    target_resource: str
    environment: str | None
    inactivity_days: int | None
    confidence: float


class _StrictRiskSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_mismatch: bool
    missing_constraints: list[str]
    scope_class: str
    production_impact: bool
    irreversible: bool
    uncertainty: float
    rationale: list[str]


_INTENT_SYSTEM_PROMPT = (
    "You extract structured intent from a user instruction about deleting "
    "or deactivating user accounts. Respond with ONLY a single JSON object, no prose, no "
    "markdown fences, matching exactly this shape: "
    '{"action_type": string, "target_resource": string, '
    '"environment": "test"|"production"|null, "inactivity_days": integer|null, '
    '"confidence": number}. '
    "Never include a verdict, decision, authorization, allow, or block field. "
    "You never approve or deny actions; you only extract intent."
)

_RISK_SYSTEM_PROMPT = (
    "You extract structured semantic risk features by comparing a user's "
    "intent against a proposed tool call and its measured operational "
    "impact. The provided ImpactEnvelope counts are authoritative -- never "
    "invent, restate, or override them, and never include any row-count "
    "field in your response. Respond with ONLY a single JSON object, no "
    "prose, no markdown fences, matching exactly this shape: "
    '{"intent_mismatch": boolean, "missing_constraints": [string], '
    '"scope_class": string, "production_impact": boolean, '
    '"irreversible": boolean, "uncertainty": number, "rationale": [string]}. '
    "Never include a verdict, decision, authorization, allow, or block field. "
    "You never approve or deny actions; you only extract risk features."
)


def _extract_json_object(text: str) -> dict:
    """Parse text as strictly one JSON object -- prose around it is
    rejected (json.loads raises on anything but a bare JSON document)."""
    parsed = json.loads(text.strip())
    if not isinstance(parsed, dict):
        raise ValueError("Nebius response was not a JSON object.")
    return parsed


def _reject_forbidden_keys(raw: dict) -> None:
    if {k.lower() for k in raw} & FORBIDDEN_RESPONSE_KEYS:
        raise ValueError("Response contains a forbidden verdict/decision-style field.")


def _call_nebius_json(client, system_prompt: str, user_prompt: str, model_name: str) -> dict:
    response = client.chat.completions.create(
        model=model_name,
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("Empty Nebius response.")
    raw = _extract_json_object(content)
    _reject_forbidden_keys(raw)
    return raw


_SECRET_MARKERS = ("authorization:", "bearer ", "api_key", "apikey", "token=", "sk-")


def _sanitize_exception_message(exc: Exception) -> str:
    """Concise, secret-safe summary for a stderr degradation log -- never
    a full traceback, never a raw header/token value."""
    text = str(exc)
    lowered = text.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        return "(details withheld to avoid leaking credentials)"
    return text[:200]


def _degraded_reason_for(exc: Exception) -> Literal["upstream_failure", "invalid_response"]:
    if isinstance(exc, (ValidationError, ValueError)):
        return "invalid_response"
    return "upstream_failure"


def extract_intent_live_or_fallback(instruction: str, client=None) -> ExtractionOutcome:
    """Attempt live Nebius intent extraction; fall back to the
    deterministic extractor on any failure. Never raises."""
    should_attempt = client is not None or (live_enabled() and has_api_key())
    degraded_reason: Literal["missing_configuration", "upstream_failure", "invalid_response"] | None = None

    if should_attempt:
        try:
            active_client = client or _build_client()
            model_name = model()
            raw = _call_nebius_json(
                active_client,
                _INTENT_SYSTEM_PROMPT,
                f"User instruction: {instruction}",
                model_name,
            )
            validated = _StrictIntentSchema.model_validate(raw)
            if validated.environment not in _SUPPORTED_INTENT_ENVIRONMENTS:
                raise ValueError(f"Unsupported environment: {validated.environment!r}")
            intent = IntentConstraints(
                action_type=_canonicalize_action_type(validated.action_type),
                target_resource=_canonicalize_target_resource(validated.target_resource),
                environment=validated.environment,
                inactivity_days=validated.inactivity_days,
                confidence=validated.confidence,
            )
            return ExtractionOutcome(value=intent, mode="nebius_live", model=model_name)
        except Exception as exc:  # noqa: BLE001 -- any failure must fall back, never crash
            degraded_reason = _degraded_reason_for(exc)
            logger.warning(
                "Nebius live intent extraction failed (%s); falling back to deterministic "
                "extraction: %s",
                degraded_reason,
                _sanitize_exception_message(exc),
            )
    else:
        # should_attempt is False only when client is None and live Nebius
        # isn't configured/enabled -- no attempt was made at all.
        degraded_reason = "missing_configuration"

    return ExtractionOutcome(
        value=extract_intent(instruction),
        mode="deterministic_fallback",
        model=None,
        degraded_reason=degraded_reason,
    )


def extract_risk_features_live_or_fallback(
    instruction: str,
    intent: IntentConstraints,
    proposed_arguments: dict,
    impact: ImpactEnvelope,
    client=None,
) -> ExtractionOutcome:
    """Attempt live Nebius risk-feature extraction; fall back to the
    deterministic extractor on any failure. Never raises."""
    should_attempt = client is not None or (live_enabled() and has_api_key())
    degraded_reason: Literal["missing_configuration", "upstream_failure", "invalid_response"] | None = None

    if should_attempt:
        try:
            active_client = client or _build_client()
            model_name = model()
            user_prompt = (
                f"Original instruction: {instruction}\n"
                f"Validated intent: {intent.model_dump_json()}\n"
                f"Proposed tool arguments: {json.dumps(proposed_arguments)}\n"
                "Authoritative ImpactEnvelope (source of truth for counts, "
                f"never override it): {impact.model_dump_json()}"
            )
            raw = _call_nebius_json(active_client, _RISK_SYSTEM_PROMPT, user_prompt, model_name)
            validated = _StrictRiskSchema.model_validate(raw)
            # scope_class, production_impact, and irreversible are derived
            # from the authoritative ImpactEnvelope, never from the model --
            # Nebius contributes only the semantic fields below.
            risk = RiskFeatures(
                intent_mismatch=validated.intent_mismatch,
                missing_constraints=validated.missing_constraints,
                scope_class=_scope_class_for_count(impact.estimated_count),
                production_impact=impact.environment_counts.get("production", 0) > 0,
                irreversible=impact.hard_delete,
                uncertainty=validated.uncertainty,
                rationale=validated.rationale,
            )
            return ExtractionOutcome(value=risk, mode="nebius_live", model=model_name)
        except Exception as exc:  # noqa: BLE001 -- any failure must fall back, never crash
            degraded_reason = _degraded_reason_for(exc)
            logger.warning(
                "Nebius live risk-feature extraction failed (%s); falling back to "
                "deterministic extraction: %s",
                degraded_reason,
                _sanitize_exception_message(exc),
            )
    else:
        degraded_reason = "missing_configuration"

    return ExtractionOutcome(
        value=extract_risk_features(intent, proposed_arguments, impact),
        mode="deterministic_fallback",
        model=None,
        degraded_reason=degraded_reason,
    )
