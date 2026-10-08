"""Seeded local permission provider.

Stands in for the firm's IAM / ethical-wall source, which stays
authoritative in a real deployment; this layer only reads its state. No
network, no external API, no model call. Restricted documents are a
coarse permission-layer fact here, not an Action Boundary rule.
"""

import datetime

from legal.corpus import CANONICAL_MATTER, EXPANSION_MATTER, build_corpus
from legal.models import PermissionSnapshot

SEEDED_USER_ID = "associate-01"
SEEDED_SOURCE_VERSION = "seed-v1"
SEEDED_PERMITTED_RECIPIENTS = frozenset({"counsel@example.com"})


def get_permission_snapshot(user_id: str, captured_at: datetime.datetime) -> PermissionSnapshot | None:
    """The seeded snapshot for the one seeded user, or None for anyone else.

    The seeded user may access M-102 and M-118 but not M-131. Restricted
    document IDs are the corpus documents behind an ethical wall.
    """
    if user_id != SEEDED_USER_ID:
        return None
    return PermissionSnapshot(
        user_id=user_id,
        permitted_matters=frozenset({CANONICAL_MATTER, EXPANSION_MATTER}),
        restricted_document_ids=frozenset(
            document.doc_id for document in build_corpus() if document.restricted
        ),
        permitted_recipients=SEEDED_PERMITTED_RECIPIENTS,
        source_version=SEEDED_SOURCE_VERSION,
        captured_at=captured_at,
    )
