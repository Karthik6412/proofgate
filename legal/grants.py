"""AuthorityGrant validity facts.

Reports only whether a grant is present and in effect at a given instant:
a grant is VALID exactly during [granted_at, expires_at).
It is not an authorization decision and never returns ALLOW/BLOCK; the
caller supplies `now` explicitly, so the result never depends on a hidden
clock.
"""

import datetime
from enum import Enum

from legal.models import AuthorityGrant


class GrantValidity(str, Enum):
    VALID = "VALID"
    NOT_YET_VALID = "NOT_YET_VALID"
    EXPIRED = "EXPIRED"
    MISSING = "MISSING"


def grant_validity(grant: AuthorityGrant | None, now: datetime.datetime) -> GrantValidity:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if grant is None:
        return GrantValidity.MISSING
    if now < grant.granted_at:
        return GrantValidity.NOT_YET_VALID
    if now >= grant.expires_at:
        return GrantValidity.EXPIRED
    return GrantValidity.VALID
