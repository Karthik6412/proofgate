"""Real snapshot creation and server-side snapshot metadata store.

create_snapshot lives in the Operations module (per the PRD's Operations
Module contract): it produces a real recoverable copy of working.db and
persists authoritative metadata keyed by snapshot_id. It does not
evaluate policy, intent, or proof -- interpreting that metadata to decide
whether a caller-supplied RollbackProof is valid is ProofGate's job (see
proofgate/proofs.py). Never authorize using this module's data alone.
"""

import datetime
import json
import shutil
import uuid
from pathlib import Path

from operations.database import OPERATIONS_DIR, PRISTINE_DB_PATH, WORKING_DB_PATH
from proofgate.models import RollbackProof
from proofgate.selector import compute_selector_hash

SNAPSHOTS_DIR = OPERATIONS_DIR / "snapshots"


def _metadata_path(snapshot_id: str) -> Path:
    return SNAPSHOTS_DIR / f"{snapshot_id}.meta.json"


def _snapshot_db_path(snapshot_id: str) -> Path:
    return SNAPSHOTS_DIR / f"{snapshot_id}.db"


def create_snapshot(
    resource: str,
    inactive_days: int,
    environment: str | None,
    max_affected_rows: int,
    db_path: Path = WORKING_DB_PATH,
) -> RollbackProof:
    """Create a real recoverable copy of db_path and persist its metadata.

    snapshot_id is generated server-side; the caller cannot choose it.
    """
    if Path(db_path).resolve() == PRISTINE_DB_PATH.resolve():
        raise ValueError("Refusing to snapshot the pristine database.")

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    snapshot_id = f"snap-{uuid.uuid4().hex[:12]}"
    selector_hash = compute_selector_hash(
        {"inactive_days": inactive_days, "environment": environment}
    )

    snapshot_path = _snapshot_db_path(snapshot_id)
    shutil.copyfile(db_path, snapshot_path)

    metadata = {
        "snapshot_id": snapshot_id,
        "resource": resource,
        "selector_hash": selector_hash,
        "max_affected_rows": max_affected_rows,
        "snapshot_path": str(snapshot_path),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    _metadata_path(snapshot_id).write_text(json.dumps(metadata, indent=2))

    return RollbackProof(
        snapshot_id=snapshot_id,
        resource=resource,
        selector_hash=selector_hash,
        max_affected_rows=max_affected_rows,
    )


def get_snapshot_metadata(snapshot_id: str) -> dict | None:
    """Retrieve authoritative server-side snapshot metadata, or None if unknown."""
    path = _metadata_path(snapshot_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())
