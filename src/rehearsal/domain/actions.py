"""Day 1 allowlist of typed action intents."""

from typing import Annotated, Literal

from pydantic import Field, field_validator

from rehearsal.domain.models import FrozenModel, IssueId, Label

ActionKind = Literal["close_issue", "add_label"]


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


Action = Annotated[CloseIssue | AddLabel, Field(discriminator="kind")]
