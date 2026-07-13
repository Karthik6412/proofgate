"""Real trusted snapshot restoration (Slice 23).

create_snapshot (operations/snapshots.py) copies the entire working.db
file at snapshot time -- the artifact genuinely is a complete SQLite
copy, not a selector-scoped row list. restore_snapshot below treats that
full copy as an honest data source but only ever *syncs* the rows this
function can safely vouch for: the environment='test' rows of the
`users` table. Production rows are never inserted, updated, or otherwise
touched by this function, by construction -- not merely by convention.
This is deliberately narrower than "replace the whole file": it gives
real per-row conflict/idempotency semantics (one transaction, reject on
conflict, no-op when nothing changed) while still recovering everything
the artifact can honestly restore, since the demo's injected mismatch is
always a test row.

This module is policy-unaware, exactly like operations/actions.py: it
never decides whether rollback is authorized, never grades its own
restoration (independent verification is proofgate.rollback's job, using
its own separately-computed digest -- see compute_users_digest below,
which is a read-only diagnostic reused by both sides, not a verdict),
and never returns ALLOW/BLOCK or ROLLED_BACK/MANUAL_REVIEW_REQUIRED.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

from operations.database import WORKING_DB_PATH, get_connection
from operations.snapshots import SNAPSHOTS_DIR, get_snapshot_metadata
from proofgate.models import RestoreResult
from proofgate.selector import compute_selector_hash

_USERS_COLUMNS = ("id", "email", "environment", "last_login", "status", "deleted_at", "row_version")


def _rejected(status: str, failure_category: str) -> RestoreResult:
    return RestoreResult(
        attempted=True,
        success=False,
        status=status,
        restored_count=0,
        test_rows_restored=0,
        production_rows_restored=0,
        already_present_count=0,
        conflict_count=0,
        verification_passed=False,
        failure_category=failure_category,
    )


def _snapshot_integrity_ok(snapshot_path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            return result == "ok"
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def compute_users_digest(db_path: Path, environment: str | None = None) -> str:
    """Independent-verification digest: sorted complete row records ->
    canonical JSON -> SHA-256. Deliberately separate from
    proofgate.selector's canonical_selector_json/compute_selector_hash --
    a selector hash binds a proof to its request arguments; this digest
    instead fingerprints actual row *contents*, and the two must never be
    conflated or share an implementation."""
    conn = get_connection(db_path)
    try:
        query = "SELECT * FROM users WHERE deleted_at IS NULL"
        params: list = []
        if environment is not None:
            query += " AND environment = ?"
            params.append(environment)
        query += " ORDER BY id"
        rows = [dict(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def restore_snapshot(snapshot_id: str, *, expected_selector: dict) -> RestoreResult:
    """Restore operations/working.db's test-environment `users` rows from
    the validated snapshot identified by snapshot_id.

    Never restores to any destination other than WORKING_DB_PATH, never
    touches operations/pristine.db, never inserts/updates a production
    row, and never partially commits: any detected conflict rolls back
    the entire transaction.
    """
    metadata = get_snapshot_metadata(snapshot_id)
    if metadata is None:
        return _rejected("REJECTED_INVALID_SNAPSHOT", "snapshot metadata not found")

    if metadata.get("resource") != "users":
        return _rejected("REJECTED_INVALID_SNAPSHOT", "snapshot resource is not 'users'")

    snapshot_path = Path(metadata["snapshot_path"])
    try:
        snapshot_path.resolve().relative_to(SNAPSHOTS_DIR.resolve())
    except ValueError:
        return _rejected("REJECTED_INVALID_SNAPSHOT", "snapshot path outside the trusted snapshots directory")

    if not snapshot_path.exists():
        return _rejected("REJECTED_INVALID_SNAPSHOT", "snapshot artifact file is missing")

    expected_hash = compute_selector_hash(expected_selector)
    if expected_hash != metadata.get("selector_hash"):
        return _rejected("REJECTED_SELECTOR_MISMATCH", "expected selector does not match the snapshot's bound selector")

    if expected_selector.get("environment") != "test":
        return _rejected("REJECTED_UNSAFE_SCOPE", "automatic restoration is limited to environment='test'")

    if not _snapshot_integrity_ok(snapshot_path):
        return _rejected("REJECTED_INVALID_SNAPSHOT", "snapshot artifact failed integrity check")

    try:
        snap_conn = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
        snap_conn.row_factory = sqlite3.Row
        try:
            snapshot_rows = [
                dict(row)
                for row in snap_conn.execute(
                    "SELECT * FROM users WHERE environment = 'test' ORDER BY id"
                ).fetchall()
            ]
        finally:
            snap_conn.close()
    except sqlite3.DatabaseError:
        return _rejected("REJECTED_INVALID_SNAPSHOT", "snapshot artifact could not be read")

    try:
        return _sync_test_rows_into_working_db(snapshot_rows)
    except Exception:
        # Fail closed on any unexpected error (e.g. a constraint violation)
        # rather than letting an exception escape this function's typed
        # contract or exposing a raw traceback to a caller.
        return RestoreResult(
            attempted=True,
            success=False,
            status="FAILED",
            restored_count=0,
            test_rows_restored=0,
            production_rows_restored=0,
            already_present_count=0,
            conflict_count=0,
            verification_passed=False,
            failure_category="unexpected error during restoration",
        )


def _sync_test_rows_into_working_db(snapshot_rows: list[dict]) -> RestoreResult:
    conn = get_connection(WORKING_DB_PATH, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current_rows = {
                row["id"]: dict(row)
                for row in conn.execute("SELECT * FROM users WHERE environment = 'test'").fetchall()
            }

            to_insert = []
            already_present_count = 0
            conflict_count = 0
            for srow in snapshot_rows:
                rid = srow["id"]
                if rid not in current_rows:
                    to_insert.append(srow)
                elif current_rows[rid] == srow:
                    already_present_count += 1
                else:
                    conflict_count += 1

            if conflict_count > 0:
                conn.rollback()
                return RestoreResult(
                    attempted=True,
                    success=False,
                    status="REJECTED_CONFLICT",
                    restored_count=0,
                    test_rows_restored=0,
                    production_rows_restored=0,
                    already_present_count=already_present_count,
                    conflict_count=conflict_count,
                    verification_passed=False,
                    failure_category="existing row content conflicts with the snapshot's row content",
                )

            if not to_insert:
                conn.rollback()  # nothing to change; safe no-op
                return RestoreResult(
                    attempted=True,
                    success=True,
                    status="ALREADY_RESTORED",
                    restored_count=0,
                    test_rows_restored=0,
                    production_rows_restored=0,
                    already_present_count=already_present_count,
                    conflict_count=0,
                    verification_passed=True,
                )

            for srow in to_insert:
                conn.execute(
                    f"INSERT INTO users ({', '.join(_USERS_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _USERS_COLUMNS)})",
                    [srow[col] for col in _USERS_COLUMNS],
                )

            # Sanity re-query within the same transaction: confirm every
            # inserted row is now present with matching content. This is
            # restore_snapshot's own operational check, not the
            # independent verification proofgate.rollback performs
            # separately using its own pre-computed digest.
            reloaded = {
                row["id"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM users WHERE environment = 'test'"
                ).fetchall()
            }
            verification_passed = all(reloaded.get(srow["id"]) == srow for srow in to_insert)

            if not verification_passed:
                conn.rollback()
                return RestoreResult(
                    attempted=True,
                    success=False,
                    status="VERIFICATION_FAILED",
                    restored_count=0,
                    test_rows_restored=0,
                    production_rows_restored=0,
                    already_present_count=already_present_count,
                    conflict_count=0,
                    verification_passed=False,
                    failure_category="post-insert re-query did not match the inserted rows",
                )

            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    return RestoreResult(
        attempted=True,
        success=True,
        status="RESTORED",
        restored_count=len(to_insert),
        test_rows_restored=len(to_insert),
        production_rows_restored=0,
        already_present_count=already_present_count,
        conflict_count=0,
        verification_passed=True,
    )
