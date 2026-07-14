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

_plan_environment_change is the one shared predicate preview_set_feature_
flag (read) and set_feature_flag (write) both use, mirroring
operations.selection.build_predicate's "one shared selection semantics"
discipline exactly -- so read and write paths can never diverge on what
counts as an affected/changed environment.

Deterministic rounding rule (documented and tested): for an environment
actually targeted by the request, if the requested (enabled,
rollout_percentage) differs from that environment's current recorded
state, affected = audience * rollout_percentage // 100 (integer floor);
otherwise the environment is an untouched no-op with zero impact.

Documented limitation: this simplified rule does not attempt to diff a
shrinking rollout against previously-exposed users -- a request that
disables a flag (rollout_percentage=0) is honestly counted as
affecting 0 users by this formula, even though it is still recognized
and recorded as a real configuration change (its version still
increments). A real feature-flag provider's actual exposed-audience
change on a rollout decrease is a materially harder problem intentionally
out of scope for this slice.
"""

import datetime
import json
import os
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


def _plan_environment_change(
    current: dict, enabled: bool, rollout_percentage: int, audience: int
) -> tuple[bool, int]:
    """The one shared rule preview and mutation both use. Returns
    (changed, affected) for a single environment. changed is true only
    when the requested (enabled, rollout_percentage) differs from the
    current recorded state for this environment; affected is the
    deterministic floor-rounded count, zero for an untouched no-op."""
    changed = (current["enabled"], current["rollout_percentage"]) != (enabled, rollout_percentage)
    affected = (audience * rollout_percentage // 100) if changed else 0
    return changed, affected


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
        _, affected = _plan_environment_change(flag_state[env], enabled, rollout_percentage, audience[env])
        environment_counts[env] = affected

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
    for env in _target_environments(environment):
        changed, affected = _plan_environment_change(flag_state[env], enabled, rollout_percentage, audience[env])
        if changed:
            flag_state[env]["enabled"] = enabled
            flag_state[env]["rollout_percentage"] = rollout_percentage
            flag_state[env]["version"] += 1
        if env == "production":
            production_affected = affected
        else:
            test_affected = affected

    _atomic_write_json(working_path, state)

    return MutationResult(
        affected_count=production_affected + test_affected,
        production_affected=production_affected,
        test_affected=test_affected,
    )
