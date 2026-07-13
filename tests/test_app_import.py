"""Plain import check for app.py.

Run in a subprocess (not a direct `import app` in this test process) so
app.py's module-level side effects (resetting the real working database,
preparing CRAFT evidence) stay isolated from the rest of the test suite
and don't pollute Python's module cache across test files.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_app_module_imports_without_raising():
    result = subprocess.run(
        [sys.executable, "-c", "import app"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"import app failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_importing_app_never_attempts_a_live_craft_call_even_with_real_env_credentials():
    """Slice 20 regression guard. Verified empirically during Slice 20
    that, without this guard, `import app` in this repo's own real .env
    configuration (real CRAFT_PROJECT_ID + CRAFT_LIVE_ENABLED=true) does
    reach craft.client.run_craft_workflow -- which opens a real browser
    OAuth window with no cross-process token cache. app.py now always
    forces CRAFT_LIVE_ENABLED=false immediately before its one automatic,
    ungated, top-level evidence-preparation call, regardless of the
    resolved runtime mode. This subprocess deliberately does NOT set
    NEBIUS_API_KEY/CRAFT_PROJECT_ID/PROOFGATE_RUNTIME_MODE itself, so that
    python-dotenv's load_dotenv() (triggered by importing
    agent.nebius_client) loads this repo's real .env file exactly as a
    fresh, un-instrumented `streamlit run app.py` would.
    """
    probe_script = (
        "import craft.client as client_module\n"
        "_called = {'v': False}\n"
        "def _tracking(*a, **k):\n"
        "    _called['v'] = True\n"
        "    raise RuntimeError('intercepted -- would have made a real call')\n"
        "client_module.run_craft_workflow = _tracking\n"
        "import craft.evidence as evidence_module\n"
        "evidence_module.run_craft_workflow = _tracking\n"
        "import app\n"
        "assert _called['v'] is False, 'app import reached run_craft_workflow'\n"
        "print('OK: no live CRAFT call attempted during import')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe_script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(Path.home())},
    )
    assert result.returncode == 0, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK: no live CRAFT call attempted during import" in result.stdout
