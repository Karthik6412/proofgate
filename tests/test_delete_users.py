"""Slice 2 tests: dumb operations.delete_users, unprotected direct hard delete.

Uses tmp_path-isolated pristine/working databases except for the one test
that explicitly proves the real repo operations/pristine.db is never
mutated by the reset/delete flow.
"""

import hashlib
import inspect

from operations.actions import count_rows, delete_users, preview_delete_users
from operations.database import PRISTINE_DB_PATH, reset_working_db
from operations.seed import build_pristine_db


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_broad_direct_delete_matches_demo_numbers(tmp_path):
    pristine_path = tmp_path / "pristine.db"
    working_path = tmp_path / "working.db"
    build_pristine_db(pristine_path)
    reset_working_db(pristine_path=pristine_path, working_path=working_path)

    result = delete_users(inactive_days=90, environment=None, db_path=working_path)

    assert result.affected_count == 10073
    assert result.production_affected == 9981
    assert result.test_affected == 92
    assert count_rows(working_path) == 550


def test_corrected_direct_delete_matches_demo_numbers(tmp_path):
    pristine_path = tmp_path / "pristine.db"
    working_path = tmp_path / "working.db"
    build_pristine_db(pristine_path)
    reset_working_db(pristine_path=pristine_path, working_path=working_path)

    result = delete_users(inactive_days=90, environment="test", db_path=working_path)

    assert result.affected_count == 92
    assert result.production_affected == 0
    assert result.test_affected == 92
    assert count_rows(working_path) == 10623 - 92
    assert count_rows(working_path, environment="production") == 9981 + 500


def test_real_pristine_db_unchanged_by_reset_and_delete(tmp_path):
    before_hash = _sha256(PRISTINE_DB_PATH)

    working_path = tmp_path / "working.db"
    reset_working_db(pristine_path=PRISTINE_DB_PATH, working_path=working_path)
    delete_users(inactive_days=90, environment=None, db_path=working_path)

    after_hash = _sha256(PRISTINE_DB_PATH)
    assert before_hash == after_hash


def test_reset_restores_exact_counts_and_delete_is_deterministic(tmp_path):
    pristine_path = tmp_path / "pristine.db"
    working_path = tmp_path / "working.db"
    build_pristine_db(pristine_path)

    reset_working_db(pristine_path=pristine_path, working_path=working_path)
    assert count_rows(working_path) == 10623
    first = delete_users(inactive_days=90, environment=None, db_path=working_path)

    reset_working_db(pristine_path=pristine_path, working_path=working_path)
    assert count_rows(working_path) == 10623
    second = delete_users(inactive_days=90, environment=None, db_path=working_path)

    assert first == second


def test_delete_users_accepts_no_policy_or_proof_parameters():
    params = set(inspect.signature(delete_users).parameters)
    assert params == {"inactive_days", "environment", "db_path"}
    for forbidden in (
        "action_context",
        "rollback_proof",
        "policy",
        "verdict",
        "risk_score",
    ):
        assert forbidden not in params


def test_preview_and_delete_use_identical_selection_semantics(tmp_path):
    pristine_path = tmp_path / "pristine.db"
    working_path = tmp_path / "working.db"
    build_pristine_db(pristine_path)
    reset_working_db(pristine_path=pristine_path, working_path=working_path)

    envelope = preview_delete_users(
        inactive_days=90, environment=None, db_path=working_path
    )
    result = delete_users(inactive_days=90, environment=None, db_path=working_path)

    assert envelope.estimated_count == result.affected_count
    assert envelope.environment_counts["production"] == result.production_affected
    assert envelope.environment_counts["test"] == result.test_affected
