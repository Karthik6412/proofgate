"""Slice 20.1 tests: explicit, lazy, user-initiated live CRAFT fetch in the
Streamlit app.

Runs against the real operations/working.db and real CRAFT evidence cache
path, like the rest of this repo's AppTest-based integration tests (see
test_app_interaction.py). Each test resets the working database and the
CRAFT evidence cache path before/after to stay isolated; live CRAFT is
never actually reached in these tests -- either because no real
CRAFT_PROJECT_ID/credentials exist in the test environment, or because a
test explicitly injects a failure via monkeypatching
craft.evidence.run_craft_workflow (never the real network client).
"""

import pytest

from streamlit.testing.v1 import AppTest

pytestmark = pytest.mark.usefixtures(
    "_isolate_audit_log",
    "_disable_live_nebius_by_default",
    "_isolate_craft_by_default",
    "_force_fallback_runtime_mode",
)

FETCH_BUTTON_LABEL = "Fetch live CRAFT evidence"


def _click(at: AppTest, label: str) -> None:
    for button in at.button:
        if button.label == label:
            button.click().run()
            return
    raise AssertionError(f"No button labeled {label!r} found. Buttons: {[b.label for b in at.button]}")


def _craft_caption(at: AppTest) -> str:
    for c in at.caption:
        if c.value.startswith("**CRAFT evidence:**"):
            return c.value
    raise AssertionError("CRAFT evidence status caption not found")


@pytest.fixture(autouse=True)
def _reset_real_backend():
    from operations.database import reset_working_db

    reset_working_db()
    yield
    reset_working_db()


# ---------------------------------------------------------------------------
# LIVE mode: button visibility, no call before click, one call after click,
# no repeat on rerun, reset clears state.
# ---------------------------------------------------------------------------


def test_live_mode_shows_cached_evidence_and_fetch_button_before_any_click(monkeypatch):
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "live")

    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()

    assert not at.exception
    assert FETCH_BUTTON_LABEL in {b.label for b in at.button}
    assert "not requested" in _craft_caption(at)
    assert at.session_state["craft_live_requested"] is False
    assert at.session_state["craft_live_outcome"] is None


def test_no_live_craft_call_before_explicit_click(monkeypatch):
    import craft.evidence as evidence_module

    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "live")

    def _explode(*_a, **_k):
        raise AssertionError("must not attempt live CRAFT before the explicit button click")

    monkeypatch.setattr(evidence_module, "run_craft_workflow", _explode)

    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()
    assert not at.exception

    # Also exercise an unrelated interaction -- still no live call.
    _click(at, "Run unsafe agent action")
    assert not at.exception


def test_clicking_fetch_invokes_live_workflow_exactly_once_and_labels_result(monkeypatch):
    import craft.evidence as evidence_module

    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "live")
    call_count = {"n": 0}
    real = evidence_module.prepare_craft_evidence

    def _counting(*args, **kwargs):
        call_count["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(evidence_module, "prepare_craft_evidence", _counting)
    import app_logic

    monkeypatch.setattr(app_logic, "prepare_craft_evidence", _counting, raising=False)

    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()
    calls_after_load = call_count["n"]

    _click(at, FETCH_BUTTON_LABEL)
    assert not at.exception
    assert call_count["n"] == calls_after_load + 1
    assert at.session_state["craft_live_requested"] is True
    assert FETCH_BUTTON_LABEL not in {b.label for b in at.button}

    # No real CRAFT_PROJECT_ID exists in the test environment, so this
    # gracefully lands on cached-fallback -- never fabricated as live.
    assert "Live" not in _craft_caption(at) or "not requested" not in _craft_caption(at)
    caption = _craft_caption(at)
    assert caption != "**CRAFT evidence:** Live"


def test_rerun_and_unrelated_clicks_do_not_repeat_the_live_call(monkeypatch):
    import craft.evidence as evidence_module

    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "live")
    call_count = {"n": 0}
    real = evidence_module.prepare_craft_evidence

    def _counting(*args, **kwargs):
        call_count["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(evidence_module, "prepare_craft_evidence", _counting)
    import app_logic

    monkeypatch.setattr(app_logic, "prepare_craft_evidence", _counting, raising=False)

    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()

    _click(at, FETCH_BUTTON_LABEL)
    count_after_click = call_count["n"]

    _click(at, "Run unsafe agent action")
    assert call_count["n"] == count_after_click

    at.run()  # plain rerun, no new interaction
    assert call_count["n"] == count_after_click


def test_reset_clears_live_evidence_presentation_state(monkeypatch):
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "live")

    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()
    _click(at, FETCH_BUTTON_LABEL)
    assert at.session_state["craft_live_requested"] is True

    _click(at, "Reset demo")
    assert not at.exception
    assert at.session_state["craft_live_requested"] is False
    assert at.session_state["craft_live_outcome"] is None
    assert at.session_state["craft_live_error"] is None
    assert FETCH_BUTTON_LABEL in {b.label for b in at.button}


def test_no_secrets_appear_after_explicit_fetch(monkeypatch):
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "live")
    monkeypatch.setenv("CRAFT_PROJECT_ID", "dummy-project")

    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()
    _click(at, FETCH_BUTTON_LABEL)
    assert not at.exception

    all_text = "\n".join(c.value for c in at.caption) + "\n".join(m.value for m in at.markdown)
    for marker in ("Authorization:", "Bearer ", "access_token", "refresh_token", "client_secret"):
        assert marker not in all_text


# ---------------------------------------------------------------------------
# Live failure behavior
# ---------------------------------------------------------------------------


def test_live_failure_degrades_to_fallback_and_is_labeled_honestly(monkeypatch):
    import craft.evidence as evidence_module

    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "live")
    monkeypatch.setenv("CRAFT_LIVE_ENABLED", "true")
    monkeypatch.setenv("CRAFT_PROJECT_ID", "dummy-project")

    def _explode(*_a, **_k):
        raise ConnectionError("simulated upstream failure")

    monkeypatch.setattr(evidence_module, "run_craft_workflow", _explode)

    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()
    _click(at, FETCH_BUTTON_LABEL)

    assert not at.exception
    assert at.session_state["craft_live_outcome"].mode != "live"
    caption = _craft_caption(at)
    assert "Live" not in caption or caption == "**CRAFT evidence:** Cached fallback — live retrieval failed"


def test_live_failure_reason_is_sanitized(monkeypatch):
    import craft.evidence as evidence_module

    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "live")
    monkeypatch.setenv("CRAFT_LIVE_ENABLED", "true")
    monkeypatch.setenv("CRAFT_PROJECT_ID", "dummy-project")

    def _explode(*_a, **_k):
        raise RuntimeError("Authorization: Bearer sk-should-never-appear-anywhere rejected")

    monkeypatch.setattr(evidence_module, "run_craft_workflow", _explode)

    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()
    _click(at, FETCH_BUTTON_LABEL)

    all_text = "\n".join(c.value for c in at.caption) + "\n".join(m.value for m in at.markdown)
    assert "sk-should-never-appear-anywhere" not in all_text


def test_failed_fetch_does_not_retry_automatically_on_rerun(monkeypatch):
    import craft.evidence as evidence_module

    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "live")
    monkeypatch.setenv("CRAFT_LIVE_ENABLED", "true")
    monkeypatch.setenv("CRAFT_PROJECT_ID", "dummy-project")
    call_count = {"n": 0}

    def _explode(*_a, **_k):
        call_count["n"] += 1
        raise ConnectionError("simulated upstream failure")

    monkeypatch.setattr(evidence_module, "run_craft_workflow", _explode)

    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()
    _click(at, FETCH_BUTTON_LABEL)
    assert call_count["n"] == 1

    at.run()
    _click(at, "Run reversible broad call")
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Deterministic modes never expose or trigger live CRAFT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["fallback", "reliable_demo"])
def test_deterministic_modes_never_show_fetch_button(monkeypatch, mode):
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", mode)

    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()

    assert not at.exception
    assert FETCH_BUTTON_LABEL not in {b.label for b in at.button}


@pytest.mark.parametrize("mode", ["fallback", "reliable_demo"])
def test_deterministic_modes_never_call_live_craft(monkeypatch, mode):
    import craft.evidence as evidence_module

    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", mode)
    monkeypatch.setenv("CRAFT_PROJECT_ID", "dummy-project")

    def _explode(*_a, **_k):
        raise AssertionError(f"must not attempt live CRAFT in {mode} mode")

    monkeypatch.setattr(evidence_module, "run_craft_workflow", _explode)

    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()
    assert not at.exception

    _click(at, "Run unsafe agent action")
    assert not at.exception
