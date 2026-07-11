"""Deterministic SQLite schema and pristine-to-working reset.

All mutations during a demo run target only working.db. pristine.db is
built once (see seed.py) and is never mutated afterward; reset_working_db
restores working.db by copying pristine.db over it.
"""

import shutil
import sqlite3
from pathlib import Path

OPERATIONS_DIR = Path(__file__).resolve().parent
PRISTINE_DB_PATH = OPERATIONS_DIR / "pristine.db"
WORKING_DB_PATH = OPERATIONS_DIR / "working.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL,
    environment TEXT NOT NULL,
    last_login TEXT NOT NULL,
    status TEXT NOT NULL,
    deleted_at TEXT,
    row_version INTEGER NOT NULL
);
"""


def get_connection(db_path: Path, isolation_level: str | None = "") -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=isolation_level)
    conn.row_factory = sqlite3.Row
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA_SQL)
    conn.commit()


def reset_working_db(
    pristine_path: Path = PRISTINE_DB_PATH,
    working_path: Path = WORKING_DB_PATH,
) -> None:
    """Restore working.db to the exact pristine seeded state."""
    if Path(working_path).resolve() == Path(PRISTINE_DB_PATH).resolve():
        raise ValueError("Refusing to overwrite the pristine database as a reset target.")
    if not pristine_path.exists():
        raise FileNotFoundError(
            f"Pristine database not found at {pristine_path}. Run seed.py first."
        )
    shutil.copyfile(pristine_path, working_path)
