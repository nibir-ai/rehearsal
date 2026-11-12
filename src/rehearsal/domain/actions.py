"""Allowlist of typed IssueHub action intents."""

from typing import Annotated, Literal

from pydantic import Field, field_validator

from rehearsal.domain.models import FrozenModel, IssueId, Label, canonical_labels

ActionKind = Literal["close_issue", "add_label", "add_comment", "create_issue"]


class CloseIssue(FrozenModel):
    kind: Literal["close_issue"] = "close_issue"
    issue_id: IssueId
    reason: Literal["completed", "not_planned"]


class AddLabel(FrozenModel):
    kind: Literal["add_label"] = "add_label"
    issue_id: IssueId
    label: Label

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        return value.lower()


class AddComment(FrozenModel):
    kind: Literal["add_comment"] = "add_comment"
    issue_id: IssueId
    body: str = Field(min_length=1, max_length=2000)

    @field_validator("body")
    @classmethod
    def check_body(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("comment must contain text without null characters")
        return value


class CreateIssue(FrozenModel):
    kind: Literal["create_issue"] = "create_issue"
    title: str = Field(min_length=4, max_length=160)
    body: str = Field(max_length=4000)
    labels: tuple[Label, ...] = Field(default=(), max_length=3)
    source_issue_id: IssueId | None = None

    _normalize_labels = field_validator("labels")(canonical_labels)

    @field_validator("title")
    @classmethod
    def check_title(cls, value: str) -> str:
        if len(value.strip()) < 4 or not value.isprintable():
            raise ValueError("title must contain at least four printable characters")
        return value

    @field_validator("body")
    @classmethod
    def check_body(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("issue body must not contain null characters")
        return value


Action = Annotated[CloseIssue | AddLabel | AddComment | CreateIssue, Field(discriminator="kind")]


def referenced_issue_id(action: Action) -> str | None:
    """The existing issue read by the action, if any."""
    return action.source_issue_id if isinstance(action, CreateIssue) else action.issue_id
