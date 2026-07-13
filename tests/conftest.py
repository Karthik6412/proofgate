"""Project-wide test isolation.

guarded_delete_users has a frozen signature and cannot take a test-only
audit path. proofgate.audit reads DEFAULT_AUDIT_PATH from its module
namespace at call time (not as a bound default-argument value), so
redirecting it here via monkeypatch keeps every test -- old and new --
from ever writing to the real runtime artifacts/audit.jsonl.

Live Nebius is force-disabled by default for the same reason: guarded_
delete_users has no test-only seam, so without this, any test exercising
it would attempt a real network call whenever the ambient shell actually
has NEBIUS_API_KEY exported, regardless of that test's intent. Tests that
specifically exercise the live-Nebius path re-enable it explicitly within
their own body.

Live CRAFT is disabled and the CRAFT evidence cache is redirected the same
way. guarded_delete_users never calls CRAFT itself, but craft.evidence.
prepare_craft_evidence is a real function tests call directly, and this
keeps every test from touching the real artifacts/craft_evidence_cache.json
or attempting real OAuth/network unless a test explicitly opts back in.

Slice 20: PROOFGATE_RUNTIME_MODE is additionally forced to "fallback" for
every test. proofgate.runtime_mode.resolve_mode() checks this variable
before any legacy flag, so this is the authoritative, explicit way the
test suite stays offline -- not just an accident of the legacy flags
above also happening to be set to "false". Tests that specifically
exercise the live path (Nebius or CRAFT) re-enable it explicitly within
their own body, exactly as they already do for the legacy flags.
"""

import pytest

import proofgate.audit as audit_module


@pytest.fixture(autouse=True)
def _isolate_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_module, "DEFAULT_AUDIT_PATH", tmp_path / "audit.jsonl")


@pytest.fixture(autouse=True)
def _disable_live_nebius_by_default(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "false")
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _isolate_craft_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("CRAFT_LIVE_ENABLED", "false")
    monkeypatch.delenv("CRAFT_PROJECT_ID", raising=False)
    monkeypatch.setenv("CRAFT_CACHE_PATH", str(tmp_path / "craft_evidence_cache.json"))


@pytest.fixture(autouse=True)
def _force_fallback_runtime_mode(monkeypatch):
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "fallback")
