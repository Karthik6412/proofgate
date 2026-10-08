"""Legal domain fact models.

All models are frozen, and every multi-valued field is an immutable,
explicitly normalized collection, so a fact cannot be widened or rewritten
after construction. Digests use the one shared canonical encoding in
proofgate.selector; sets are sorted into lists before hashing, never
handed to canonical_json directly.
"""

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from proofgate.selector import sha256_digest


def _require_timezone(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value


class Document(BaseModel):
    """One synthetic legal document.

    matter_id is the principal workflow boundary. privileged is
    deterministic metadata; restricted represents an external ethical-wall
    result. canary is a seeded marker for later continuation work and
    carries no logic here. related_doc_ids are directed one-hop links,
    stored sorted and de-duplicated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    doc_id: str
    matter_id: str
    client_id: str
    title: str
    status: Literal["draft", "final", "archived"]
    document_type: str
    privileged: bool
    restricted: bool
    canary: str | None
    text: str
    related_doc_ids: tuple[str, ...] = ()

    @field_validator("related_doc_ids")
    @classmethod
    def _normalize_related(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def _no_self_link(self) -> "Document":
        if self.doc_id in self.related_doc_ids:
            raise ValueError(f"{self.doc_id} links to itself")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_hash(self) -> str:
        """Derived from text, so it can never disagree with the content."""
        return sha256_digest(self.text)


class AuthorityGrant(BaseModel):
    """What the human actually authorized for one workflow.

    Trusted input supplied by the grantor, never inferred from or rewritten
    by an agent plan. allowed_recipients is stored as a sorted, de-duplicated
    tuple; recipients are compared exactly as given (no case folding), so
    normalization can never widen the grant.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    matter_id: str = Field(min_length=1)
    max_documents: int = Field(gt=0)
    privilege_policy: Literal["exclude", "include"]
    allowed_recipients: tuple[str, ...]
    granted_by: str = Field(min_length=1)
    granted_at: datetime.datetime
    expires_at: datetime.datetime

    @field_validator("allowed_recipients")
    @classmethod
    def _normalize_recipients(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @field_validator("granted_at", "expires_at")
    @classmethod
    def _timezone_aware(cls, value: datetime.datetime) -> datetime.datetime:
        return _require_timezone(value)

    @model_validator(mode="after")
    def _expiry_after_issue(self) -> "AuthorityGrant":
        if self.expires_at <= self.granted_at:
            raise ValueError("expires_at must be after granted_at")
        return self


class PermissionSnapshot(BaseModel):
    """Coarse access-control state consumed from the permission source.

    Permission, not authority: which matters the user may access, which
    documents an ethical wall restricts, and which recipients may receive
    documents. The digest covers user, permitted matters, restricted
    document IDs, permitted recipients and source version, with each set
    sorted. captured_at records freshness and is excluded, so an unchanged
    refresh does not change the digest.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1)
    permitted_matters: frozenset[str]
    restricted_document_ids: frozenset[str]
    permitted_recipients: frozenset[str]
    source_version: str = Field(min_length=1)
    captured_at: datetime.datetime

    @field_validator("captured_at")
    @classmethod
    def _timezone_aware(cls, value: datetime.datetime) -> datetime.datetime:
        return _require_timezone(value)

    def canonical_state(self) -> dict:
        return {
            "user_id": self.user_id,
            "permitted_matters": sorted(self.permitted_matters),
            "restricted_document_ids": sorted(self.restricted_document_ids),
            "permitted_recipients": sorted(self.permitted_recipients),
            "source_version": self.source_version,
        }

    @computed_field  # type: ignore[prop-decorator]
    @property
    def snapshot_digest(self) -> str:
        return sha256_digest(self.canonical_state())
