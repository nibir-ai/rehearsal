"""Deterministic guards over typed intents and the current predicted state."""

import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import Field, field_validator

from rehearsal.domain.actions import (
    Action,
    ActionKind,
    AddComment,
    AddLabel,
    CloseIssue,
    CreateIssue,
)
from rehearsal.domain.models import BoardSnapshot, FrozenModel, Issue, Label, canonical_labels

MAX_ACTIONS = 5
_RESTRICTED_TEXT = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b"
)


class BlockReason(StrEnum):
    TARGET_OUT_OF_SCOPE = "TARGET_OUT_OF_SCOPE"
    ACTION_LIMIT_EXCEEDED = "ACTION_LIMIT_EXCEEDED"
    CREATE_LIMIT_EXCEEDED = "CREATE_LIMIT_EXCEEDED"
    ACTION_NOT_ALLOWLISTED = "ACTION_NOT_ALLOWLISTED"
    ISSUE_NOT_FOUND = "ISSUE_NOT_FOUND"
    PROTECTED_LABEL = "PROTECTED_LABEL"
    ALREADY_CLOSED = "ALREADY_CLOSED"
    RECENT_ACTIVITY = "RECENT_ACTIVITY"
    MISSING_RESOLUTION_EVIDENCE = "MISSING_RESOLUTION_EVIDENCE"
    UNKNOWN_LABEL = "UNKNOWN_LABEL"
    RESTRICTED_CONTENT = "RESTRICTED_CONTENT"
    SNAPSHOT_CAPACITY_EXCEEDED = "SNAPSHOT_CAPACITY_EXCEEDED"
    TEMPORARY_ID_COLLISION = "TEMPORARY_ID_COLLISION"


class Policy(FrozenModel):
    policy_id: str = Field(default="issuehub-maintenance", min_length=1, max_length=64)
    policy_version: str = Field(default="policy-v2", min_length=1, max_length=64)
    allowed_board_id: str = "demo/platform"
    permitted_actions: tuple[ActionKind, ...] = (
        "close_issue",
        "add_label",
        "add_comment",
        "create_issue",
    )
    protected_labels: tuple[Label, ...] = ("legal-hold", "needs-human", "security")
    max_actions: int = Field(default=MAX_ACTIONS, ge=0, le=MAX_ACTIONS)
    max_new_issues: int = Field(default=2, ge=0, le=MAX_ACTIONS)
    minimum_inactivity_days: int = Field(default=30, ge=30, le=timedelta.max.days)

    _normalize_labels = field_validator("protected_labels")(canonical_labels)


def check_batch(
    snapshot: BoardSnapshot, actions: Sequence[Action], policy: Policy
) -> BlockReason | None:
    if snapshot.board_id != policy.allowed_board_id:
        return BlockReason.TARGET_OUT_OF_SCOPE
    if len(actions) > policy.max_actions:
        return BlockReason.ACTION_LIMIT_EXCEEDED
    if sum(isinstance(action, CreateIssue) for action in actions) > policy.max_new_issues:
        return BlockReason.CREATE_LIMIT_EXCEEDED
    return None


def has_restricted_content(action: Action) -> bool:
    if isinstance(action, CreateIssue):
        return bool(_RESTRICTED_TEXT.search(action.title) or _RESTRICTED_TEXT.search(action.body))
    if isinstance(action, AddComment):
        return bool(_RESTRICTED_TEXT.search(action.body))
    return False


def evaluate_action(
    action: Action,
    issue: Issue | None,
    label_registry: tuple[Label, ...],
    policy: Policy,
    clock: datetime,
) -> BlockReason | None:
    if action.kind not in policy.permitted_actions:
        return BlockReason.ACTION_NOT_ALLOWLISTED
    if has_restricted_content(action):
        return BlockReason.RESTRICTED_CONTENT
    if isinstance(action, CreateIssue):
        if action.source_issue_id is not None and issue is None:
            return BlockReason.ISSUE_NOT_FOUND
        if not set(action.labels).issubset(label_registry):
            return BlockReason.UNKNOWN_LABEL
        return None
    if issue is None:
        return BlockReason.ISSUE_NOT_FOUND
    if isinstance(action, CloseIssue):
        if not set(policy.protected_labels).isdisjoint(issue.labels):
            return BlockReason.PROTECTED_LABEL
        if issue.state != "open":
            return BlockReason.ALREADY_CLOSED
        if clock - issue.last_activity_at < timedelta(days=policy.minimum_inactivity_days):
            return BlockReason.RECENT_ACTIVITY
        if not issue.resolved_by_maintainer:
            return BlockReason.MISSING_RESOLUTION_EVIDENCE
    elif isinstance(action, AddLabel) and action.label not in label_registry:
        return BlockReason.UNKNOWN_LABEL
    return None
