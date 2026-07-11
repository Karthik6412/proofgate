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
