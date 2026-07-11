"""Deterministic seed data for the pristine shadow-CRM database.

Seed counts (fixed, no random variation):
    9,981 inactive production users
       92 inactive test users
      500 active production users
       50 active test users
"""

import datetime
from pathlib import Path

from operations.config import DEMO_REFERENCE_DATE
from operations.database import PRISTINE_DB_PATH, create_schema, get_connection

SEED_COUNTS = {
    ("production", "inactive"): 9981,
    ("test", "inactive"): 92,
    ("production", "active"): 500,
    ("test", "active"): 50,
}

# Inactive last_login is well past any reasonable inactive_days threshold
# (90 in the demo); active last_login is well within it. The margins are
# wide so the seeded classification stays stable regardless of which day
# the demo/tests are run on.
INACTIVE_DAYS_AGO = 100
ACTIVE_DAYS_AGO = 10


def _iso_date(days_ago: int) -> str:
    return (DEMO_REFERENCE_DATE - datetime.timedelta(days=days_ago)).isoformat()


def build_pristine_db(path: Path = PRISTINE_DB_PATH) -> None:
    """Create (or overwrite) the pristine database with the deterministic seed."""
    if path.exists():
        path.unlink()

    conn = get_connection(path)
    try:
        create_schema(conn)

        inactive_last_login = _iso_date(INACTIVE_DAYS_AGO)
        active_last_login = _iso_date(ACTIVE_DAYS_AGO)

        rows = []
        counter = 0
        for (environment, activity), count in SEED_COUNTS.items():
            last_login = (
                inactive_last_login if activity == "inactive" else active_last_login
            )
            for _ in range(count):
                counter += 1
                email = f"user{counter}@{environment}.example.com"
                rows.append((email, environment, last_login, "active", None, 1))

        conn.executemany(
            """
            INSERT INTO users
                (email, environment, last_login, status, deleted_at, row_version)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    build_pristine_db()
    print(f"Built pristine database at {PRISTINE_DB_PATH}")
