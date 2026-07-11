"""Shared demo-wide constants.

DEMO_REFERENCE_DATE is the single fixed "today" used by seed generation,
preview selection, and deletion selection, so the demo's inactive/active
classification never drifts with the wall clock.
"""

from datetime import date

DEMO_REFERENCE_DATE = date(2026, 7, 11)
