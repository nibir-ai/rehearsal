"""Immutable IssueHub records and stable snapshot serialization."""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from rehearsal.domain.canonical import fingerprint

IssueId = Annotated[str, Field(min_length=1, max_length=64)]
Label = Annotated[str, Field(min_length=1, max_length=64)]
Revision = Annotated[int, Field(ge=1)]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def as_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def canonical_labels(labels: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(sorted(label.lower() for label in labels))
    if len(set(normalized)) != len(normalized):
        raise ValueError("labels must be unique after normalization")
    return normalized


class Issue(FrozenModel):
    id: IssueId
    board_id: str
    title: str
    body: str = ""
    state: Literal["open", "closed"]
    labels: tuple[Label, ...] = ()
    last_activity_at: AwareDatetime
    revision: Revision
    resolved_by_maintainer: bool = False

    _normalize_activity = field_validator("last_activity_at")(as_utc)
    _normalize_labels = field_validator("labels")(canonical_labels)


class BoardSnapshot(FrozenModel):
    snapshot_id: str
    board_id: str
    board_revision: Revision
    captured_at: AwareDatetime
    issues: tuple[Issue, ...] = Field(max_length=100)
    label_registry: tuple[Label, ...]

    _normalize_capture = field_validator("captured_at")(as_utc)
    _normalize_registry = field_validator("label_registry")(canonical_labels)

    @model_validator(mode="after")
    def check_board_integrity(self) -> Self:
        if len({issue.id for issue in self.issues}) != len(self.issues):
            raise ValueError("issue IDs must be unique within the snapshot")
        registry = set(self.label_registry)
        for issue in self.issues:
            if issue.board_id != self.board_id:
                raise ValueError("all issues must belong to the snapshot board")
            if not registry.issuperset(issue.labels):
                raise ValueError("all issue labels must exist in the board registry")
        return self

    def fingerprint(self) -> str:
        return fingerprint(self)
