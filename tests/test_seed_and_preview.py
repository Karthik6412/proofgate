"""Slice 1 tests: deterministic seed, pristine-to-working reset, preview_delete_users.

Uses tmp_path-isolated pristine/working databases so tests never depend on
or mutate the repo's real operations/pristine.db or operations/working.db.
"""

import sqlite3

from operations.actions import preview_delete_users
from operations.database import reset_working_db
from operations.seed import SEED_COUNTS, build_pristine_db
from proofgate.selector import compute_selector_hash


def _row_count(db_path, environment=None, deleted_at_is_null=True):
    conn = sqlite3.connect(str(db_path))
    try:
        query = "SELECT COUNT(*) FROM users WHERE 1=1"
        params = []
        if deleted_at_is_null:
            query += " AND deleted_at IS NULL"
        if environment is not None:
            query += " AND environment = ?"
            params.append(environment)
        return conn.execute(query, params).fetchone()[0]
    finally:
        conn.close()


def test_seed_counts_are_exact(tmp_path):
    pristine_path = tmp_path / "pristine.db"
    build_pristine_db(pristine_path)

    assert _row_count(pristine_path, environment="production") == 9981 + 500
    assert _row_count(pristine_path, environment="test") == 92 + 50
    assert _row_count(pristine_path) == 9981 + 92 + 500 + 50

    expected_total = sum(SEED_COUNTS.values())
    assert expected_total == 10623
    assert _row_count(pristine_path) == expected_total


def test_reset_restores_pristine_state(tmp_path):
    pristine_path = tmp_path / "pristine.db"
    working_path = tmp_path / "working.db"
    build_pristine_db(pristine_path)

    reset_working_db(pristine_path=pristine_path, working_path=working_path)
    assert _row_count(working_path) == 10623

    # Mutate the working copy directly (simulating a destructive run).
    conn = sqlite3.connect(str(working_path))
    conn.execute("DELETE FROM users")
    conn.commit()
    conn.close()
    assert _row_count(working_path) == 0

    # Pristine must be untouched, and reset must restore working.db exactly.
    assert _row_count(pristine_path) == 10623
    reset_working_db(pristine_path=pristine_path, working_path=working_path)
    assert _row_count(working_path) == 10623


def test_preview_broad_delete_matches_demo_numbers(tmp_path):
    pristine_path = tmp_path / "pristine.db"
    working_path = tmp_path / "working.db"
    build_pristine_db(pristine_path)
    reset_working_db(pristine_path=pristine_path, working_path=working_path)

    envelope = preview_delete_users(
        inactive_days=90, environment=None, db_path=working_path
    )

    assert envelope.estimated_count == 10073
    assert envelope.environment_counts == {"production": 9981, "test": 92}
    assert envelope.tool_name == "delete_users"
    assert envelope.hard_delete is True


def test_preview_corrected_delete_matches_demo_numbers(tmp_path):
    pristine_path = tmp_path / "pristine.db"
    working_path = tmp_path / "working.db"
    build_pristine_db(pristine_path)
    reset_working_db(pristine_path=pristine_path, working_path=working_path)

    envelope = preview_delete_users(
        inactive_days=90, environment="test", db_path=working_path
    )

    assert envelope.estimated_count == 92
    assert envelope.environment_counts == {"production": 0, "test": 92}


def test_selector_hash_is_stable_and_reflects_arguments(tmp_path):
    pristine_path = tmp_path / "pristine.db"
    working_path = tmp_path / "working.db"
    build_pristine_db(pristine_path)
    reset_working_db(pristine_path=pristine_path, working_path=working_path)

    broad = preview_delete_users(inactive_days=90, environment=None, db_path=working_path)
    broad_again = preview_delete_users(
        inactive_days=90, environment=None, db_path=working_path
    )
    corrected = preview_delete_users(
        inactive_days=90, environment="test", db_path=working_path
    )

    assert broad.selector_hash == broad_again.selector_hash
    assert broad.selector_hash != corrected.selector_hash
    assert broad.selector_hash == compute_selector_hash(
        {"inactive_days": 90, "environment": None}
    )
    assert corrected.selector_hash == compute_selector_hash(
        {"inactive_days": 90, "environment": "test"}
    )
