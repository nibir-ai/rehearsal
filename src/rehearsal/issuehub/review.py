"""Project a simulation into the data the review page needs."""

from typing import Literal

from pydantic import AwareDatetime

from rehearsal.domain.actions import Action, referenced_issue_id
from rehearsal.domain.models import FrozenModel, Revision
from rehearsal.domain.plans import Evidence, Sha256
from rehearsal.domain.policy import BlockReason
from rehearsal.domain.simulator import AllowedAction, BlockedAction, IssueFieldChange, PlanPreview


class ReviewAction(FrozenModel):
    id: str
    action: Action
    status: Literal["ALLOWED", "BLOCKED", "NO_OP"]
    issue_id: str | None
    issue_title: str | None
    current_state: Literal["open", "closed"] | None
    temporary_id: str | None = None
    reason_code: BlockReason | None = None
    changes: tuple[IssueFieldChange, ...] = ()
    evidence: tuple[Evidence, ...]


class ReviewReport(FrozenModel):
    board_id: str
    board_revision: Revision
    simulation_clock: AwareDatetime
    plan_digest: Sha256
    actions: tuple[ReviewAction, ...]


def review_report(preview: PlanPreview) -> ReviewReport:
    plan = preview.plan
    actions = []
    for entry, outcome in zip(plan.actions, preview.simulation.outcomes, strict=True):
        issue = outcome.before
        changes: tuple[IssueFieldChange, ...] = ()
        temporary_id = None
        if isinstance(outcome, AllowedAction):
            changes = outcome.changes
            if issue is None:
                issue = outcome.after
                temporary_id = issue.id
            elif outcome.comment is not None:
                temporary_id = outcome.comment.id
        actions.append(
            ReviewAction(
                id=entry.action_id,
                action=entry.action,
                status=outcome.status,
                issue_id=referenced_issue_id(entry.action),
                issue_title=issue.title if issue is not None else None,
                current_state=outcome.before.state if outcome.before is not None else None,
                temporary_id=temporary_id,
                reason_code=outcome.reason_code if isinstance(outcome, BlockedAction) else None,
                changes=changes,
                evidence=entry.evidence,
            )
        )
    return ReviewReport(
        board_id=plan.target_id,
        board_revision=plan.board_revision,
        simulation_clock=plan.simulation_clock,
        plan_digest=preview.plan_digest,
        actions=tuple(actions),
    )
