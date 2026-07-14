"""Slice 26: lightweight, high-value documentation and hygiene checks.

These are presence/consistency checks, not brittle full-document
snapshot tests -- they confirm the required documents exist, that the
registry/rule-ID facts they claim actually match the real implementation,
that a few critical safety/limitation statements are present, and that
no generated state, secret, or local absolute path has been committed.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text()


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel_path",
    [
        "README.md",
        "THREAT_MODEL.md",
        "docs/architecture.md",
        "docs/trust-model.md",
        "docs/production-evolution.md",
        "docs/interview-guide.md",
        "scripts/run_portfolio_demo.py",
        "scripts/verify_local_setup.py",
        ".env.example",
    ],
)
def test_required_document_or_script_exists(rel_path):
    assert (REPO_ROOT / rel_path).exists(), f"missing required file: {rel_path}"


# ---------------------------------------------------------------------------
# README documents the real registry
# ---------------------------------------------------------------------------


def test_readme_documents_all_registered_public_tools():
    from proofgate.registry import _REGISTRY

    readme = _read("README.md")
    for tool_name in _REGISTRY:
        assert tool_name in readme, f"README does not mention registered tool {tool_name!r}"


def test_documented_tool_count_matches_registry_count():
    from proofgate.registry import _REGISTRY

    assert len(_REGISTRY) == 3
    readme = _read("README.md")
    assert "`delete_users`" in readme
    assert "`deactivate_users`" in readme
    assert "`set_feature_flag`" in readme


def test_documented_rule_ids_match_actual_rule_constants():
    from proofgate import policy as policy_module

    actual_rule_ids = {getattr(policy_module, name) for name in dir(policy_module) if name.startswith("RULE_")}
    assert actual_rule_ids == {
        "RULE_INTENT_BOUNDARY",
        "RULE_RECOVERY_PROOF",
        "RULE_WORKFLOW_BUDGET",
        "RULE_UNKNOWN_IMPACT",
    }
    readme = _read("README.md")
    threat_model = _read("THREAT_MODEL.md")
    for rule_id in actual_rule_ids:
        assert rule_id in readme or rule_id in threat_model, f"{rule_id} not documented in README or THREAT_MODEL"


# ---------------------------------------------------------------------------
# Required exact statements / numbers
# ---------------------------------------------------------------------------


def test_not_production_ready_statement_exists():
    threat_model = _read("THREAT_MODEL.md")
    assert "not presented as production-ready infrastructure" in threat_model


def test_no_production_ready_claim_anywhere_in_key_docs():
    """Any mention of "production-ready" in these docs must be a negative
    framing (explicitly disclaiming it), never an affirmative claim."""
    negative_phrases = ("not presented as production-ready", "is not production-ready", "not production-ready")
    for rel_path in ("README.md", "THREAT_MODEL.md", "docs/architecture.md", "docs/production-evolution.md"):
        text = _read(rel_path).lower()
        if "production-ready" in text:
            assert any(phrase in text for phrase in negative_phrases), (
                f"{rel_path} mentions 'production-ready' without an explicit disclaimer"
            )


def test_rollback_budget_documented_as_93_of_100():
    readme = _read("README.md")
    assert "93/100" in readme or "93 / 100" in readme


def test_corrected_delete_budget_documented_as_92_of_100():
    readme = _read("README.md")
    assert "92/100" in readme or "92 / 100" in readme


def test_feature_flag_disable_documented_as_92():
    readme = _read("README.md")
    demo = _read("DEMO.md")
    assert "92" in readme
    assert "Corrected disable" in demo


def test_fallback_mode_documented_as_credential_free():
    readme = _read("README.md")
    assert "no credentials" in readme.lower() or "no credentials required" in readme.lower()
    assert "never requires live credentials" in readme.lower() or "never require" in readme.lower()


def test_streamlit_does_not_claim_feature_flag_exposure():
    app_source = _read("app.py")
    app_logic_source = _read("app_logic.py")
    assert "set_feature_flag" not in app_source
    assert "set_feature_flag" not in app_logic_source
    assert "feature_flag" not in app_source.lower()
    assert "feature_flag" not in app_logic_source.lower()


def test_postcondition_not_incorrectly_described_as_digest_based_everywhere():
    """Normal postcondition verification is count-based; only rollback's
    independent verification is digest-based. docs/architecture.md must
    say so explicitly, distinguishing the two."""
    architecture = _read("docs/architecture.md")
    assert "count comparison" in architecture
    assert "digest" in architecture
    assert "postcondition accuracy" in architecture.lower()


# ---------------------------------------------------------------------------
# Generated-state hygiene
# ---------------------------------------------------------------------------


def test_generated_paths_are_ignored():
    gitignore = _read(".gitignore")
    for pattern in (
        "operations/working.db",
        "operations/feature_flags_working.json",
        "operations/snapshots/",
        "artifacts/",
        ".env",
        "__pycache__/",
        ".pytest_cache/",
    ):
        assert pattern in gitignore, f"{pattern!r} is not in .gitignore"


def test_env_file_is_not_tracked():
    result = subprocess.run(["git", "ls-files", ".env"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=15)
    assert result.stdout.strip() == ""


def test_portfolio_and_demo_artifacts_are_ignored():
    result = subprocess.run(
        ["git", "check-ignore", "artifacts/portfolio_demo_example.json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0  # 0 means the path IS ignored


def test_env_example_contains_placeholders_only():
    env_example = _read(".env.example")
    # No obviously-real-looking secret values -- only empty assignments,
    # "false"/"true", numeric values, or documented public URLs/model names.
    for line in env_example.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "=" in line
        _, _, value = line.partition("=")
        value = value.strip()
        # A real secret would be a long opaque token; reject anything
        # that looks like one.
        assert not re.match(r"^sk-[A-Za-z0-9]{10,}$", value)
        assert not re.match(r"^[A-Za-z0-9+/]{32,}={0,2}$", value)


def test_tracked_documentation_has_no_local_absolute_paths():
    result = subprocess.run(
        ["git", "ls-files", "*.md", "*.py"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=15
    )
    tracked_files = [f for f in result.stdout.splitlines() if f]
    offenders = []
    for rel_path in tracked_files:
        try:
            text = (REPO_ROOT / rel_path).read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if re.search(r"/Users/[A-Za-z0-9_.-]+/", text):
            offenders.append(rel_path)
    assert offenders == [], f"tracked files contain local absolute paths: {offenders}"


def test_no_real_secret_pattern_in_tracked_files():
    result = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=15)
    tracked_files = [f for f in result.stdout.splitlines() if f]
    secret_pattern = re.compile(r"AKIA[A-Z0-9]{16}|-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----")
    offenders = []
    for rel_path in tracked_files:
        path = REPO_ROOT / rel_path
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if secret_pattern.search(text):
            offenders.append(rel_path)
    assert offenders == [], f"possible secret pattern found in: {offenders}"


# ---------------------------------------------------------------------------
# Referenced scripts/paths actually exist
# ---------------------------------------------------------------------------


def test_readme_referenced_scripts_exist():
    readme = _read("README.md")
    for script in ("scripts/verify_local_setup.py", "scripts/run_portfolio_demo.py"):
        assert script in readme
        assert (REPO_ROOT / script).exists()


def test_readme_referenced_docs_exist_and_are_linked():
    readme = _read("README.md")
    for doc in (
        "docs/architecture.md",
        "docs/trust-model.md",
        "THREAT_MODEL.md",
        "docs/production-evolution.md",
        "docs/interview-guide.md",
        "DEMO.md",
    ):
        assert doc in readme, f"README does not reference {doc}"
        assert (REPO_ROOT / doc).exists()
