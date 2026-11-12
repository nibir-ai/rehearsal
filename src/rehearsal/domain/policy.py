"""Deterministic guards over typed intents and the current predicted state."""

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import Field, field_validator

from rehearsal.domain.actions import Action, ActionKind, CloseIssue
from rehearsal.domain.models import BoardSnapshot, FrozenModel, Issue, Label, canonical_labels

MAX_ACTIONS = 5


class BlockReason(StrEnum):
    TARGET_OUT_OF_SCOPE = "TARGET_OUT_OF_SCOPE"
    ACTION_LIMIT_EXCEEDED = "ACTION_LIMIT_EXCEEDED"
    ACTION_NOT_ALLOWLISTED = "ACTION_NOT_ALLOWLISTED"
    ISSUE_NOT_FOUND = "ISSUE_NOT_FOUND"
    PROTECTED_LABEL = "PROTECTED_LABEL"
    ALREADY_CLOSED = "ALREADY_CLOSED"
    RECENT_ACTIVITY = "RECENT_ACTIVITY"
    MISSING_RESOLUTION_EVIDENCE = "MISSING_RESOLUTION_EVIDENCE"
    UNKNOWN_LABEL = "UNKNOWN_LABEL"


class Policy(FrozenModel):
    policy_id: str = Field(default="issuehub-maintenance", min_length=1, max_length=64)
    policy_version: str = Field(default="policy-v1", min_length=1, max_length=64)
    allowed_board_id: str = "demo/platform"
    permitted_actions: tuple[ActionKind, ...] = ("close_issue", "add_label")
    protected_labels: tuple[Label, ...] = ("legal-hold", "needs-human", "security")
    max_actions: int = Field(default=MAX_ACTIONS, ge=0, le=MAX_ACTIONS)
    minimum_inactivity_days: int = Field(default=30, ge=30, le=timedelta.max.days)

    _normalize_labels = field_validator("protected_labels")(canonical_labels)


def check_batch(snapshot: BoardSnapshot, action_count: int, policy: Policy) -> BlockReason | None:
    if snapshot.board_id != policy.allowed_board_id:
        return BlockReason.TARGET_OUT_OF_SCOPE
    if action_count > policy.max_actions:
        return BlockReason.ACTION_LIMIT_EXCEEDED
    return None


def evaluate_action(
    action: Action,
    issue: Issue,
    label_registry: tuple[Label, ...],
    policy: Policy,
    clock: datetime,
) -> BlockReason | None:
    if action.kind not in policy.permitted_actions:
        return BlockReason.ACTION_NOT_ALLOWLISTED
    if isinstance(action, CloseIssue):
        if not set(policy.protected_labels).isdisjoint(issue.labels):
            return BlockReason.PROTECTED_LABEL
        if issue.state != "open":
            return BlockReason.ALREADY_CLOSED
        if clock - issue.last_activity_at < timedelta(days=policy.minimum_inactivity_days):
            return BlockReason.RECENT_ACTIVITY
        if not issue.resolved_by_maintainer:
            return BlockReason.MISSING_RESOLUTION_EVIDENCE
    elif action.label not in label_registry:
        return BlockReason.UNKNOWN_LABEL
    return None
