"""Slice 23 tests: the DEMO_SIMULATE_POSTCONDITION_MISMATCH demonstration
hook inside operations.actions.delete_users, tested in isolation with
tmp_path-isolated databases (same convention as tests/test_delete_users.py).
"""

import pytest

from operations.actions import count_rows, delete_users
from operations.database import PRISTINE_DB_PATH, reset_working_db
from operations.seed import build_pristine_db


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("DEMO_SIMULATE_POSTCONDITION_MISMATCH", raising=False)
    yield


def _fresh_dbs(tmp_path):
    pristine_path = tmp_path / "pristine.db"
    working_path = tmp_path / "working.db"
    build_pristine_db(pristine_path)
    reset_working_db(pristine_path=pristine_path, working_path=working_path)
    return pristine_path, working_path


def test_off_by_default(tmp_path):
    _, working_path = _fresh_dbs(tmp_path)
    result = delete_users(inactive_days=90, environment="test", db_path=working_path)
    assert result.affected_count == 92
    assert result.test_affected == 92


def test_no_effect_when_explicitly_false(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_SIMULATE_POSTCONDITION_MISMATCH", "false")
    _, working_path = _fresh_dbs(tmp_path)
    result = delete_users(inactive_days=90, environment="test", db_path=working_path)
    assert result.affected_count == 92


def test_real_mutation_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_SIMULATE_POSTCONDITION_MISMATCH", "true")
    _, working_path = _fresh_dbs(tmp_path)
    before = count_rows(working_path)

    result = delete_users(inactive_days=90, environment="test", db_path=working_path)

    assert result.affected_count == 93  # 92 intended + 1 real extra demo deletion
    assert result.test_affected == 93
    assert result.production_affected == 0
    assert count_rows(working_path) == before - 93  # a real, honest mutation


def test_never_affects_production_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_SIMULATE_POSTCONDITION_MISMATCH", "true")
    _, working_path = _fresh_dbs(tmp_path)
    before_production = count_rows(working_path, environment="production")

    delete_users(inactive_days=90, environment="test", db_path=working_path)

    assert count_rows(working_path, environment="production") == before_production


def test_cannot_target_pristine_database_even_when_enabled(monkeypatch):
    monkeypatch.setenv("DEMO_SIMULATE_POSTCONDITION_MISMATCH", "true")
    with pytest.raises(ValueError):
        delete_users(inactive_days=90, environment="test", db_path=PRISTINE_DB_PATH)


def test_hook_returns_zero_extra_when_no_eligible_test_row_remains(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_SIMULATE_POSTCONDITION_MISMATCH", "true")
    _, working_path = _fresh_dbs(tmp_path)

    # Delete every test row first (broad, unscoped -- test-only sandbox).
    delete_users(inactive_days=0, environment="test", db_path=working_path)
    assert count_rows(working_path, environment="test") == 0

    # A second call has no eligible test rows left for either the
    # predicate or the demo hook -- both real, honest zero.
    result = delete_users(inactive_days=90, environment="test", db_path=working_path)
    assert result.affected_count == 0
    assert result.test_affected == 0


def test_variable_name_matches_documented_naming_convention():
    import inspect

    import operations.actions as actions_module

    source = inspect.getsource(actions_module)
    assert "DEMO_SIMULATE_POSTCONDITION_MISMATCH" in source


def test_reliable_demo_unaffected_when_flag_absent():
    """The existing reliable-demo path never sets this flag, so its
    documented outcomes (92 test rows deleted, 0 production) are
    unaffected by this hook's mere existence."""
    import os

    assert "DEMO_SIMULATE_POSTCONDITION_MISMATCH" not in os.environ
