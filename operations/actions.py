"""Dumb Operations mutation and preview logic.

This module is policy-unaware: it does not evaluate intent, risk, proof,
or workflow budget, and it never returns ALLOW/BLOCK. It only reports and
performs the authoritative operational blast radius against the shadow
CRM. preview_delete_users and delete_users share one selection predicate
(operations.selection.build_predicate) so read and write paths can never
diverge in what they consider "affected".
"""

import datetime
import logging
import os
from pathlib import Path

from operations.database import PRISTINE_DB_PATH, WORKING_DB_PATH, get_connection
from operations.selection import build_predicate, zero_filled_counts
from proofgate.models import ImpactEnvelope, MutationResult
from proofgate.selector import compute_selector_hash

logger = logging.getLogger("operations.actions")


def _demo_simulate_postcondition_mismatch_enabled() -> bool:
    """Slice 23's explicit, off-by-default demonstration hook. Never
    consulted unless a caller opts in; absent/false is a full no-op, so
    normal execution and the existing reliable demo are unaffected unless
    this is explicitly set."""
    return os.environ.get("DEMO_SIMULATE_POSTCONDITION_MISMATCH", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _demo_inject_extra_test_row_deletion(conn) -> int:
    """Deletes exactly one additional, real test-environment row that the
    caller's own selector did not target (any remaining test row -- e.g.
    an active one -- since every row the actual DELETE predicate matched
    is already gone from the table by the time this runs). Returns the
    count actually deleted (0 or 1; 0 if no such row remains). Never
    touches a production row -- the query is hardcoded to
    environment='test'. This performs a real mutation; it never fabricates
    a MutationResult or PostconditionResult -- the caller (delete_users)
    honestly folds this real extra count into what it actually reports."""
    row = conn.execute(
        "SELECT id FROM users WHERE environment = 'test' AND deleted_at IS NULL ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return 0
    cursor = conn.execute("DELETE FROM users WHERE id = ?", [row["id"]])
    logger.warning(
        "DEMO_SIMULATE_POSTCONDITION_MISMATCH is enabled: deliberately deleted one additional "
        "real test row (id=%s) outside the requested selector, to demonstrate genuine "
        "postcondition-mismatch detection and automatic rollback. This is a real database "
        "mutation, not a fabricated result.",
        row["id"],
    )
    return cursor.rowcount


def _select_environment_counts(
    conn, inactive_days: int, environment: str | None
) -> dict[str, int]:
    clause, params = build_predicate(inactive_days, environment)
    rows = conn.execute(
        f"SELECT environment, COUNT(*) AS n FROM users WHERE {clause} GROUP BY environment",
        params,
    ).fetchall()
    return zero_filled_counts(rows)


def preview_delete_users(
    inactive_days: int,
    environment: str | None,
    db_path: Path = WORKING_DB_PATH,
) -> ImpactEnvelope:
    """Compute the exact affected-row impact for a delete_users call. Read-only."""
    conn = get_connection(db_path)
    try:
        environment_counts = _select_environment_counts(conn, inactive_days, environment)
    finally:
        conn.close()

    estimated_count = sum(environment_counts.values())
    selector_hash = compute_selector_hash(
        {"inactive_days": inactive_days, "environment": environment}
    )

    return ImpactEnvelope(
        tool_name="delete_users",
        estimated_count=estimated_count,
        environment_counts=environment_counts,
        hard_delete=True,
        reversibility="irreversible_without_snapshot",
        selector_hash=selector_hash,
        generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )


def delete_users(
    inactive_days: int,
    environment: str | None,
    db_path: Path = WORKING_DB_PATH,
) -> MutationResult:
    """Dumb hard-delete mutation.

    Policy-unaware by design: accepts no ActionContext, rollback proof,
    policy configuration, or verdict. Selects, counts, and deletes the
    matching rows inside one transaction using the same predicate as
    preview_delete_users.
    """
    if Path(db_path).resolve() == PRISTINE_DB_PATH.resolve():
        raise ValueError("Refusing to mutate the pristine database.")

    conn = get_connection(db_path, isolation_level=None)
    try:
        clause, params = build_predicate(inactive_days, environment)
        conn.execute("BEGIN IMMEDIATE")
        try:
            environment_counts = _select_environment_counts(
                conn, inactive_days, environment
            )
            cursor = conn.execute(f"DELETE FROM users WHERE {clause}", params)
            expected = sum(environment_counts.values())
            if cursor.rowcount != expected:
                raise RuntimeError(
                    f"Selection/delete mismatch: counted {expected}, deleted {cursor.rowcount}"
                )

            demo_extra_test_deleted = 0
            if _demo_simulate_postcondition_mismatch_enabled():
                demo_extra_test_deleted = _demo_inject_extra_test_row_deletion(conn)

            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    return MutationResult(
        affected_count=sum(environment_counts.values()) + demo_extra_test_deleted,
        production_affected=environment_counts["production"],
        test_affected=environment_counts["test"] + demo_extra_test_deleted,
    )


def _select_deactivation_environment_counts(
    conn, inactive_days: int, environment: str | None
) -> dict[str, int]:
    """Same shared predicate as _select_environment_counts, narrowed to
    exclude rows already marked deactivated -- so repeated deactivation
    calls only ever count/affect rows that are not yet deactivated."""
    clause, params = build_predicate(inactive_days, environment)
    clause += " AND status != 'deactivated'"
    rows = conn.execute(
        f"SELECT environment, COUNT(*) AS n FROM users WHERE {clause} GROUP BY environment",
        params,
    ).fetchall()
    return zero_filled_counts(rows)


def preview_deactivate_users(
    inactive_days: int,
    environment: str | None,
    db_path: Path = WORKING_DB_PATH,
) -> ImpactEnvelope:
    """Compute the exact affected-row impact for a deactivate_users call.

    Read-only, reversible: deactivation preserves every row, so this is
    hard_delete=False. environment_counts already exclude rows that are
    already deactivated.
    """
    conn = get_connection(db_path)
    try:
        environment_counts = _select_deactivation_environment_counts(
            conn, inactive_days, environment
        )
    finally:
        conn.close()

    estimated_count = sum(environment_counts.values())
    selector_hash = compute_selector_hash(
        {"inactive_days": inactive_days, "environment": environment}
    )

    return ImpactEnvelope(
        tool_name="deactivate_users",
        estimated_count=estimated_count,
        environment_counts=environment_counts,
        hard_delete=False,
        reversibility="reversible",
        selector_hash=selector_hash,
        generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )


def deactivate_users(
    inactive_days: int,
    environment: str | None,
    db_path: Path = WORKING_DB_PATH,
) -> MutationResult:
    """Dumb reversible-status mutation.

    Policy-unaware by design, same as delete_users: accepts no
    ActionContext, rollback proof, policy configuration, or verdict. Sets
    status='deactivated' on matching rows without deleting them, using the
    same shared predicate (narrowed to exclude already-deactivated rows)
    as preview_deactivate_users -- so this is idempotent: a repeated
    identical call affects 0 rows because none remain eligible.
    """
    if Path(db_path).resolve() == PRISTINE_DB_PATH.resolve():
        raise ValueError("Refusing to mutate the pristine database.")

    conn = get_connection(db_path, isolation_level=None)
    try:
        clause, params = build_predicate(inactive_days, environment)
        clause += " AND status != 'deactivated'"
        conn.execute("BEGIN IMMEDIATE")
        try:
            environment_counts = _select_deactivation_environment_counts(
                conn, inactive_days, environment
            )
            cursor = conn.execute(
                f"UPDATE users SET status = 'deactivated' WHERE {clause}", params
            )
            expected = sum(environment_counts.values())
            if cursor.rowcount != expected:
                raise RuntimeError(
                    f"Selection/deactivate mismatch: counted {expected}, updated {cursor.rowcount}"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    return MutationResult(
        affected_count=sum(environment_counts.values()),
        production_affected=environment_counts["production"],
        test_affected=environment_counts["test"],
    )


def count_rows(db_path: Path = WORKING_DB_PATH, environment: str | None = None) -> int:
    """Count non-deleted rows, optionally filtered by environment. Test/demo helper."""
    conn = get_connection(db_path)
    try:
        query = "SELECT COUNT(*) AS n FROM users WHERE deleted_at IS NULL"
        params: list = []
        if environment is not None:
            query += " AND environment = ?"
            params.append(environment)
        return conn.execute(query, params).fetchone()["n"]
    finally:
        conn.close()
