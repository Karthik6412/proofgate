"""Slice 23 tests: operations/restoration.py -- restore_snapshot and
compute_users_digest.

Follows the same convention as tests/test_snapshot_and_allow.py:
restore_snapshot has no test-only db_path seam (it always targets the
real operations/working.db, matching create_snapshot's own real-artifact
design), so these tests reset that real file and the real snapshots
directory before/after each test.
"""

import sqlite3
from pathlib import Path

import pytest

from operations.actions import count_rows, delete_users
from operations.database import PRISTINE_DB_PATH, WORKING_DB_PATH, get_connection, reset_working_db
from operations.restoration import compute_users_digest, restore_snapshot
from operations.snapshots import SNAPSHOTS_DIR, create_snapshot, get_snapshot_metadata


@pytest.fixture(autouse=True)
def _reset_real_working_db():
    reset_working_db()
    yield
    reset_working_db()


def _corrected_snapshot(max_affected_rows: int = 92):
    return create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=max_affected_rows)


# ---------------------------------------------------------------------------
# Snapshot suitability (Part A inspection made real, asserted here)
# ---------------------------------------------------------------------------


def test_snapshot_artifact_is_a_complete_sqlite_copy_not_a_row_list():
    proof = _corrected_snapshot()
    metadata = get_snapshot_metadata(proof.snapshot_id)
    snapshot_path = Path(metadata["snapshot_path"])

    conn = sqlite3.connect(str(snapshot_path))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()

    assert columns == {"id", "email", "environment", "last_login", "status", "deleted_at", "row_version"}
    assert total == 10623  # the whole table, not just the 92 selector-matched rows


def test_snapshot_contains_every_test_row_needed_for_restoration():
    proof = _corrected_snapshot()
    metadata = get_snapshot_metadata(proof.snapshot_id)
    snapshot_test_count = count_rows(Path(metadata["snapshot_path"]), environment="test")
    assert snapshot_test_count == count_rows(WORKING_DB_PATH, environment="test")


def test_integrity_check_passes_for_untampered_artifact():
    proof = _corrected_snapshot()
    result = restore_snapshot(proof.snapshot_id, expected_selector={"inactive_days": 90, "environment": "test"})
    # An untampered, unused snapshot restoring into an unmutated working.db
    # has nothing to fix -- a safe no-op, not a rejection.
    assert result.status == "ALREADY_RESTORED"
    assert result.success is True


def test_corrupted_snapshot_is_rejected():
    proof = _corrected_snapshot()
    metadata = get_snapshot_metadata(proof.snapshot_id)
    Path(metadata["snapshot_path"]).write_bytes(b"not a real sqlite file")

    result = restore_snapshot(proof.snapshot_id, expected_selector={"inactive_days": 90, "environment": "test"})
    assert result.status == "REJECTED_INVALID_SNAPSHOT"
    assert result.success is False


def test_missing_snapshot_file_is_rejected():
    proof = _corrected_snapshot()
    metadata = get_snapshot_metadata(proof.snapshot_id)
    Path(metadata["snapshot_path"]).unlink()

    result = restore_snapshot(proof.snapshot_id, expected_selector={"inactive_days": 90, "environment": "test"})
    assert result.status == "REJECTED_INVALID_SNAPSHOT"


def test_unknown_snapshot_id_is_rejected():
    result = restore_snapshot("snap-does-not-exist", expected_selector={"inactive_days": 90, "environment": "test"})
    assert result.status == "REJECTED_INVALID_SNAPSHOT"
    assert result.success is False


def test_selector_binding_mismatch_is_rejected():
    proof = _corrected_snapshot()
    result = restore_snapshot(proof.snapshot_id, expected_selector={"inactive_days": 30, "environment": "test"})
    assert result.status == "REJECTED_SELECTOR_MISMATCH"


def test_production_scoped_restore_is_rejected():
    proof = create_snapshot(resource="users", inactive_days=90, environment="production", max_affected_rows=9981)
    result = restore_snapshot(proof.snapshot_id, expected_selector={"inactive_days": 90, "environment": "production"})
    assert result.status == "REJECTED_UNSAFE_SCOPE"


def test_broad_null_scoped_restore_is_rejected():
    proof = create_snapshot(resource="users", inactive_days=90, environment=None, max_affected_rows=10073)
    result = restore_snapshot(proof.snapshot_id, expected_selector={"inactive_days": 90, "environment": None})
    assert result.status == "REJECTED_UNSAFE_SCOPE"


def test_restore_snapshot_has_no_pristine_db_parameter():
    """restore_snapshot cannot be pointed at an arbitrary destination --
    it has no db_path parameter at all, unlike the Operations preview/
    mutation functions."""
    import inspect

    params = set(inspect.signature(restore_snapshot).parameters)
    assert "db_path" not in params
    assert "destination" not in params


def test_wrong_resource_snapshot_is_rejected():
    # No non-"users" resource exists in this codebase; simulate one by
    # tampering the persisted metadata file directly (server-side data,
    # never caller-controlled in the real flow).
    proof = _corrected_snapshot()
    metadata = get_snapshot_metadata(proof.snapshot_id)
    meta_path = SNAPSHOTS_DIR / f"{proof.snapshot_id}.meta.json"
    import json

    metadata["resource"] = "not_users"
    meta_path.write_text(json.dumps(metadata))

    result = restore_snapshot(proof.snapshot_id, expected_selector={"inactive_days": 90, "environment": "test"})
    assert result.status == "REJECTED_INVALID_SNAPSHOT"


# ---------------------------------------------------------------------------
# Restoration success: pre-mutation / post-mutation / post-restoration
# ---------------------------------------------------------------------------


def test_restoration_recovers_a_hard_deleted_test_row():
    proof = _corrected_snapshot()
    pre_mutation_digest = compute_users_digest(WORKING_DB_PATH, environment="test")
    pre_mutation_total = count_rows(WORKING_DB_PATH)

    delete_users(inactive_days=90, environment="test")
    post_mutation_digest = compute_users_digest(WORKING_DB_PATH, environment="test")
    assert post_mutation_digest != pre_mutation_digest
    assert count_rows(WORKING_DB_PATH) == pre_mutation_total - 92

    result = restore_snapshot(proof.snapshot_id, expected_selector={"inactive_days": 90, "environment": "test"})

    assert result.success is True
    assert result.status == "RESTORED"
    assert result.restored_count == 92
    assert result.test_rows_restored == 92
    assert result.production_rows_restored == 0
    assert result.conflict_count == 0

    post_restoration_digest = compute_users_digest(WORKING_DB_PATH, environment="test")
    assert post_restoration_digest == pre_mutation_digest
    assert count_rows(WORKING_DB_PATH) == pre_mutation_total
    assert count_rows(WORKING_DB_PATH, environment="production") == 9981 + 500


def test_restoration_never_touches_production_rows():
    proof = _corrected_snapshot()
    before_production_digest = compute_users_digest(WORKING_DB_PATH, environment="production")

    delete_users(inactive_days=90, environment="test")
    restore_snapshot(proof.snapshot_id, expected_selector={"inactive_days": 90, "environment": "test"})

    after_production_digest = compute_users_digest(WORKING_DB_PATH, environment="production")
    assert after_production_digest == before_production_digest


def test_pristine_db_untouched_by_restoration():
    import hashlib

    before_hash = hashlib.sha256(PRISTINE_DB_PATH.read_bytes()).hexdigest()
    proof = _corrected_snapshot()
    delete_users(inactive_days=90, environment="test")
    restore_snapshot(proof.snapshot_id, expected_selector={"inactive_days": 90, "environment": "test"})
    after_hash = hashlib.sha256(PRISTINE_DB_PATH.read_bytes()).hexdigest()
    assert before_hash == after_hash


# ---------------------------------------------------------------------------
# Idempotency (Part J)
# ---------------------------------------------------------------------------


def test_second_restore_is_a_safe_no_op():
    proof = _corrected_snapshot()
    delete_users(inactive_days=90, environment="test")

    first = restore_snapshot(proof.snapshot_id, expected_selector={"inactive_days": 90, "environment": "test"})
    assert first.status == "RESTORED"
    assert first.restored_count == 92

    digest_after_first = compute_users_digest(WORKING_DB_PATH, environment="test")

    second = restore_snapshot(proof.snapshot_id, expected_selector={"inactive_days": 90, "environment": "test"})
    assert second.status == "ALREADY_RESTORED"
    assert second.success is True
    assert second.restored_count == 0
    # All 142 snapshot-bound test rows (the 92 restored plus the 50
    # already-active test rows untouched by the delete) now match exactly.
    assert second.already_present_count == 142

    digest_after_second = compute_users_digest(WORKING_DB_PATH, environment="test")
    assert digest_after_second == digest_after_first  # no duplicates, no changed rows


# ---------------------------------------------------------------------------
# Conflict safety (Part J)
# ---------------------------------------------------------------------------


def test_conflicting_row_is_rejected_and_transaction_rolls_back():
    proof = _corrected_snapshot()
    delete_users(inactive_days=90, environment="test")

    metadata = get_snapshot_metadata(proof.snapshot_id)
    snap_conn = sqlite3.connect(str(Path(metadata["snapshot_path"])))
    try:
        deleted_row = snap_conn.execute(
            "SELECT id FROM users WHERE environment = 'test' AND deleted_at IS NULL ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        snap_conn.close()
    conflicting_id = deleted_row[0]

    # Simulate a legitimately different row having appeared at the exact
    # same primary key since the snapshot was taken, by inserting a row
    # with different content than the snapshot's copy of that same id.
    conn = get_connection(WORKING_DB_PATH, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO users (id, email, environment, last_login, status, deleted_at, row_version) "
            "VALUES (?, 'conflicting@example.com', 'test', '2020-01-01', 'active', NULL, 999)",
            [conflicting_id],
        )
        conn.commit()
    finally:
        conn.close()

    before_digest = compute_users_digest(WORKING_DB_PATH, environment="test")

    result = restore_snapshot(proof.snapshot_id, expected_selector={"inactive_days": 90, "environment": "test"})

    assert result.status == "REJECTED_CONFLICT"
    assert result.success is False
    assert result.conflict_count >= 1

    after_digest = compute_users_digest(WORKING_DB_PATH, environment="test")
    assert after_digest == before_digest  # no partial commit, unrelated rows unchanged


# ---------------------------------------------------------------------------
# compute_users_digest
# ---------------------------------------------------------------------------


def test_compute_users_digest_is_stable_for_identical_state():
    a = compute_users_digest(WORKING_DB_PATH, environment="test")
    b = compute_users_digest(WORKING_DB_PATH, environment="test")
    assert a == b


def test_compute_users_digest_changes_after_real_mutation():
    before = compute_users_digest(WORKING_DB_PATH, environment="test")
    delete_users(inactive_days=90, environment="test")
    after = compute_users_digest(WORKING_DB_PATH, environment="test")
    assert before != after


def test_compute_users_digest_is_distinct_from_selector_hash_implementation():
    """Digest and selector hash must be separate concepts/implementations
    (Part F), never sharing the canonicalization function or output."""
    from proofgate.selector import canonical_selector_json, compute_selector_hash

    import operations.restoration as restoration_module

    assert restoration_module.compute_users_digest is not canonical_selector_json
    assert restoration_module.compute_users_digest is not compute_selector_hash
    # A digest of row content and a selector hash of {inactive_days,
    # environment} are computed over entirely different inputs and must
    # not collide in any structural way.
    digest = compute_users_digest(WORKING_DB_PATH, environment="test")
    selector_hash = compute_selector_hash({"inactive_days": 90, "environment": "test"})
    assert digest != selector_hash
