"""Append-only JSONL audit trail.

Complete, not tamper-evident: no hash chaining or immutability claims.
Each guarded_delete_users invocation writes exactly one AuditEvent as a
single JSON line to the runtime audit path, which defaults to
artifacts/audit.jsonl. DEFAULT_AUDIT_PATH is read inside each function
body (not bound as a default-argument value at import time), so tests
can redirect all writes by monkeypatching it without guarded_delete_users
needing a test-only path parameter.
"""

import datetime
import uuid
from pathlib import Path

from proofgate.models import AuditEvent

SCHEMA_VERSION = "v1"
DEFAULT_AUDIT_PATH = Path("artifacts/audit.jsonl")


def new_event_id() -> str:
    """Server-side event_id generation. Never accepted from a caller."""
    return f"evt-{uuid.uuid4().hex}"


def current_timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def append_audit_event(event: AuditEvent, audit_path: Path | None = None) -> None:
    """Append exactly one JSON line. Never overwrites existing events."""
    path = Path(audit_path) if audit_path is not None else DEFAULT_AUDIT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as f:
        f.write(event.model_dump_json())
        f.write("\n")
        f.flush()


def reset_audit_log(audit_path: Path | None = None) -> None:
    """Clear only the given runtime audit file.

    Explicit demo/test utility. Never touches the database, workflow
    state, or snapshots.
    """
    path = Path(audit_path) if audit_path is not None else DEFAULT_AUDIT_PATH
    if path.exists():
        path.unlink()
