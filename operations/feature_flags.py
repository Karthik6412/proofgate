"""Deterministic filesystem-backed feature-flag resource (Slice 25).

A genuinely different resource from the users SQLite database: state
lives in local JSON files, never in operations/{pristine,working}.db.
Mirrors operations/database.py's pristine-to-working reset convention
and operations/snapshots.py's JSON-metadata precedent, but is otherwise
independent of both -- this module never imports operations.database or
operations.actions, and never touches the users table.

Audience counts (how many real users a given environment's rollout
would affect) are themselves fixed, deterministic local data -- never
derived by querying the users table -- so this resource's impact can be
computed without any cross-resource coupling. No language model is
involved in computing impact.

_plan_environment_change is the one shared calculation preview_set_
feature_flag (read) and set_feature_flag (write) both use, mirroring
operations.selection.build_predicate's "one shared selection semantics"
discipline exactly -- so read and write paths can never diverge on what
counts as an affected/changed environment.

Effective-exposure rule (Slice 25.1, documented and tested): blast
radius is the number of users whose effective feature exposure changes,
not simply the audience implied by the requested rollout percentage.
For each targeted environment:

    current_effective_percentage = current_rollout_percentage if current_enabled else 0
    requested_effective_percentage = requested_rollout_percentage if requested_enabled else 0
    current_exposed_users = audience * current_effective_percentage // 100
    requested_exposed_users = audience * requested_effective_percentage // 100
    affected_users = abs(requested_exposed_users - current_exposed_users)

This makes enabling and disabling symmetric, and rollout increases and
decreases both count only the real exposure delta (deterministic integer
floor rounding throughout). Stored configuration (enabled/rollout_
percentage/version) can still change even when effective exposure does
not -- e.g. disabled at a stored 80% moving to disabled at 0% -- because
configuration changed is a different question from user exposure
changed; the version still increments in that case, but affected_users
is honestly reported as 0.
"""

import datetime
import json
import os
from dataclasses import dataclass
from pathlib import Path

from proofgate.models import ImpactEnvelope, MutationResult
from proofgate.selector import compute_selector_hash

OPERATIONS_DIR = Path(__file__).resolve().parent
FEATURE_FLAGS_PRISTINE_PATH = OPERATIONS_DIR / "feature_flags_pristine.json"
FEATURE_FLAGS_WORKING_PATH = OPERATIONS_DIR / "feature_flags_working.json"
FEATURE_FLAG_AUDIENCE_PATH = OPERATIONS_DIR / "feature_flag_audience.json"

ENVIRONMENTS = ("test", "production")


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Feature-flag state at {path.name!r} is missing or malformed.") from exc


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically: serialize to a temp file in the same
    directory, flush and fsync, then os.replace (atomic on the same
    filesystem). The previous working file is never left partially
    written, and no partial JSON is ever left as authoritative state."""
    tmp_path = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def reset_feature_flags_working(
    pristine_path: Path = FEATURE_FLAGS_PRISTINE_PATH,
    working_path: Path = FEATURE_FLAGS_WORKING_PATH,
) -> None:
    """Restore working feature-flag state to the exact pristine seed.

    Affects only feature-flag state -- never the users database, never
    the pristine file itself. Safe to call repeatedly."""
    if Path(working_path).resolve() == Path(pristine_path).resolve():
        raise ValueError("Refusing to overwrite the pristine feature-flag file as a reset target.")
    pristine_state = _read_json(pristine_path)
    _atomic_write_json(working_path, pristine_state)


def load_audience(audience_path: Path = FEATURE_FLAG_AUDIENCE_PATH) -> dict[str, int]:
    """Deterministic, fixed audience counts -- never derived from the
    users table."""
    return _read_json(audience_path)


def _validate_flag_state_structure(state: dict, flag_name: str) -> dict:
    if not isinstance(state, dict) or flag_name not in state:
        raise ValueError(f"Unknown feature flag {flag_name!r}.")
    flag_state = state[flag_name]
    for env in ENVIRONMENTS:
        if not isinstance(flag_state, dict) or env not in flag_state:
            raise ValueError(f"Malformed feature-flag state: missing {env!r} for {flag_name!r}.")
        for key in ("enabled", "rollout_percentage", "version"):
            if key not in flag_state[env]:
                raise ValueError(f"Malformed feature-flag state: missing {key!r} for {flag_name!r}.{env}.")
    return flag_state


def _validate_request_shape(environment: str | None, rollout_percentage: int) -> None:
    """Defense in depth: the MCP layer already validates these ranges
    before Operations is ever reached, but this dumb Operations function
    must still fail safely (never with a raw KeyError) if ever called
    directly with an out-of-range value."""
    if environment is not None and environment not in ENVIRONMENTS:
        raise ValueError(f"Unsupported environment {environment!r}; must be one of {ENVIRONMENTS} or null.")
    if not (0 <= rollout_percentage <= 100):
        raise ValueError(f"rollout_percentage must be between 0 and 100 inclusive; got {rollout_percentage!r}.")


def _target_environments(environment: str | None) -> tuple[str, ...]:
    return (environment,) if environment is not None else ENVIRONMENTS


@dataclass(frozen=True)
class EnvironmentChangePlan:
    """The one shared plan preview and mutation both derive their
    numbers from -- neither ever computes affected/changed independently."""

    environment: str
    configuration_changed: bool
    current_effective_percentage: int
    requested_effective_percentage: int
    current_exposed_users: int
    requested_exposed_users: int
    affected_users: int


def _plan_environment_change(
    environment: str, current: dict, enabled: bool, rollout_percentage: int, audience: int
) -> EnvironmentChangePlan:
    """Effective-exposure planning for one environment (Slice 25.1).

    configuration_changed reflects whether the stored (enabled,
    rollout_percentage) fields would actually change -- this can be true
    even when affected_users is 0 (e.g. disabled at a stored 80% moving
    to disabled at 0%: configuration changed, but no user's effective
    exposure did). affected_users is always the absolute exposure delta,
    never a raw function of the requested rollout percentage alone.
    """
    current_effective_percentage = current["rollout_percentage"] if current["enabled"] else 0
    requested_effective_percentage = rollout_percentage if enabled else 0

    current_exposed_users = audience * current_effective_percentage // 100
    requested_exposed_users = audience * requested_effective_percentage // 100
    affected_users = abs(requested_exposed_users - current_exposed_users)

    configuration_changed = (current["enabled"], current["rollout_percentage"]) != (enabled, rollout_percentage)

    return EnvironmentChangePlan(
        environment=environment,
        configuration_changed=configuration_changed,
        current_effective_percentage=current_effective_percentage,
        requested_effective_percentage=requested_effective_percentage,
        current_exposed_users=current_exposed_users,
        requested_exposed_users=requested_exposed_users,
        affected_users=affected_users,
    )


def preview_set_feature_flag(
    *,
    flag_name: str,
    enabled: bool,
    environment: str | None,
    rollout_percentage: int,
    working_path: Path = FEATURE_FLAGS_WORKING_PATH,
    audience_path: Path = FEATURE_FLAG_AUDIENCE_PATH,
) -> ImpactEnvelope:
    """Compute the exact affected-user impact for a set_feature_flag
    call, purely from deterministic local state. Read-only: never
    mutates working state."""
    _validate_request_shape(environment, rollout_percentage)
    state = _read_json(working_path)
    flag_state = _validate_flag_state_structure(state, flag_name)
    audience = load_audience(audience_path)

    environment_counts = {"test": 0, "production": 0}
    for env in _target_environments(environment):
        plan = _plan_environment_change(env, flag_state[env], enabled, rollout_percentage, audience[env])
        environment_counts[env] = plan.affected_users

    estimated_count = sum(environment_counts.values())
    selector_hash = compute_selector_hash(
        {
            "flag_name": flag_name,
            "enabled": enabled,
            "environment": environment,
            "rollout_percentage": rollout_percentage,
        }
    )

    return ImpactEnvelope(
        tool_name="set_feature_flag",
        estimated_count=estimated_count,
        environment_counts=environment_counts,
        hard_delete=False,
        reversibility="reversible",
        selector_hash=selector_hash,
        generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )


def set_feature_flag(
    *,
    flag_name: str,
    enabled: bool,
    environment: str | None,
    rollout_percentage: int,
    working_path: Path = FEATURE_FLAGS_WORKING_PATH,
    audience_path: Path = FEATURE_FLAG_AUDIENCE_PATH,
) -> MutationResult:
    """Dumb atomic configuration-update mutation. Policy-unaware by
    design, exactly like operations.actions.delete_users/
    deactivate_users: accepts no ActionContext, rollback proof, policy
    configuration, or verdict. Never reads or writes the users database,
    never calls a model, never makes a network call. Increments version
    only for environments whose recorded state actually changes."""
    _validate_request_shape(environment, rollout_percentage)
    state = _read_json(working_path)
    flag_state = _validate_flag_state_structure(state, flag_name)
    audience = load_audience(audience_path)

    production_affected = 0
    test_affected = 0
    any_configuration_changed = False
    for env in _target_environments(environment):
        plan = _plan_environment_change(env, flag_state[env], enabled, rollout_percentage, audience[env])
        if plan.configuration_changed:
            flag_state[env]["enabled"] = enabled
            flag_state[env]["rollout_percentage"] = rollout_percentage
            flag_state[env]["version"] += 1
            any_configuration_changed = True
        if env == "production":
            production_affected = plan.affected_users
        else:
            test_affected = plan.affected_users

    # An exact no-op (every targeted environment's stored configuration
    # already matches the request) performs no write at all -- not merely
    # a write that happens to reproduce identical bytes.
    if any_configuration_changed:
        _atomic_write_json(working_path, state)

    return MutationResult(
        affected_count=production_affected + test_affected,
        production_affected=production_affected,
        test_affected=test_affected,
    )
