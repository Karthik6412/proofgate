"""Shared row-selection predicate builder.

preview_delete_users (read) and delete_users (write) must use identical
selection semantics. Both call build_predicate so the SQL WHERE clause and
cutoff-date logic exist in exactly one place.
"""

import datetime

from operations.config import DEMO_REFERENCE_DATE

ENVIRONMENTS = ("production", "test")


def build_predicate(inactive_days: int, environment: str | None) -> tuple[str, list]:
    """Build the shared WHERE-clause fragment and params for row selection.

    Selection is: not already deleted, and last_login older than the cutoff
    derived from DEMO_REFERENCE_DATE - inactive_days. environment, if given,
    narrows the selection further.
    """
    cutoff = (DEMO_REFERENCE_DATE - datetime.timedelta(days=inactive_days)).isoformat()
    clause = "deleted_at IS NULL AND last_login < ?"
    params: list = [cutoff]
    if environment is not None:
        clause += " AND environment = ?"
        params.append(environment)
    return clause, params


def zero_filled_counts(rows) -> dict[str, int]:
    """Normalize GROUP BY environment rows to always include both known
    environments, defaulting absent ones to 0."""
    counts = {env: 0 for env in ENVIRONMENTS}
    for row in rows:
        counts[row["environment"]] = row["n"]
    return counts
