"""Pure IssueHub simulation: no adapters, I/O, or ambient clock."""

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from rehearsal.domain.actions import Action, AddLabel, CloseIssue, CreateIssue, referenced_issue_id
from rehearsal.domain.canonical import fingerprint
from rehearsal.domain.models import (
    MAX_SNAPSHOT_ISSUES,
    BoardSnapshot,
    FrozenModel,
    Issue,
    IssueId,
    as_utc,
    canonical_labels,
)
from rehearsal.domain.plans import Sha256, ValidatedPlan, check_plan_context
from rehearsal.domain.policy import (
    BlockReason,
    Policy,
    check_batch,
    evaluate_action,
    has_restricted_content,
)


class PredictedComment(FrozenModel):
    id: IssueId
    issue_id: IssueId
    body: str
    created_at: AwareDatetime


class PredictedIssueLink(FrozenModel):
    issue_id: IssueId
    source_issue_id: IssueId | None


IssueFieldValue = str | int | bool | tuple[str, ...] | None


class IssueFieldChange(FrozenModel):
    field: str
    before: IssueFieldValue
    after: IssueFieldValue


class AllowedAction(FrozenModel):
    status: Literal["ALLOWED"] = "ALLOWED"
    action: Action
    before: Issue | None
    after: Issue
    comment: PredictedComment | None = None
    changes: tuple[IssueFieldChange, ...] = ()


class BlockedAction(FrozenModel):
    status: Literal["BLOCKED"] = "BLOCKED"
    action: Action | None
    reason_code: BlockReason
    before: Issue | None


class NoOpAction(FrozenModel):
    status: Literal["NO_OP"] = "NO_OP"
    action: AddLabel
    before: Issue
    after: Issue


ActionOutcome = Annotated[AllowedAction | BlockedAction | NoOpAction, Field(discriminator="status")]


class SimulationResult(FrozenModel):
    predicted_snapshot: BoardSnapshot
    predicted_fingerprint: str
    outcomes: tuple[ActionOutcome, ...]
    batch_block: BlockReason | None = None
    comments: tuple[PredictedComment, ...] = ()
    created_issues: tuple[PredictedIssueLink, ...] = ()

    @property
    def allowed(self) -> tuple[AllowedAction, ...]:
        return tuple(outcome for outcome in self.outcomes if isinstance(outcome, AllowedAction))

    @property
    def blocked(self) -> tuple[BlockedAction, ...]:
        return tuple(outcome for outcome in self.outcomes if isinstance(outcome, BlockedAction))


class PlanPreview(FrozenModel):
    plan: ValidatedPlan
    plan_digest: Sha256
    simulation: SimulationResult


class _PredictedState(FrozenModel):
    snapshot: BoardSnapshot
    comments: tuple[PredictedComment, ...]
    created_issues: tuple[PredictedIssueLink, ...]


def _issue_changes(before: Issue | None, after: Issue) -> tuple[IssueFieldChange, ...]:
    previous = before.model_dump(mode="json") if before is not None else {}
    changes = []
    for field, current in after.model_dump(mode="json").items():
        original = previous.get(field)
        if original != current:
            changes.append(
                IssueFieldChange(
                    field=field,
                    before=tuple(original) if isinstance(original, list) else original,
                    after=tuple(current) if isinstance(current, list) else current,
                )
            )
    return tuple(changes)


def simulate_plan(snapshot: BoardSnapshot, plan: ValidatedPlan, policy: Policy) -> PlanPreview:
    """Preview the exact, validated plan against the inputs bound into its digest."""
    check_plan_context(plan, snapshot, policy)
    return PlanPreview(
        plan=plan,
        plan_digest=plan.digest(),
        simulation=_simulate(
            snapshot,
            tuple((entry.action_id, entry.action) for entry in plan.actions),
            policy,
            plan.simulation_clock,
        ),
    )


def _close_issue(issue: Issue, clock: datetime) -> Issue:
    return issue.model_copy(
        update={
            "state": "closed",
            "last_activity_at": clock,
            "revision": issue.revision + 1,
        }
    )


def _add_label(issue: Issue, action: AddLabel) -> Issue:
    return issue.model_copy(
        update={
            "labels": canonical_labels((*issue.labels, action.label)),
            "revision": issue.revision + 1,
        }
    )


def simulate(
    snapshot: BoardSnapshot,
    actions: Sequence[Action],
    policy: Policy,
    clock: datetime,
) -> SimulationResult:
    """Predict a typed candidate batch against an isolated, evolving state."""
    entries = tuple((f"a{index}", action) for index, action in enumerate(actions, start=1))
    return _simulate(snapshot, entries, policy, clock)


def _simulate(
    snapshot: BoardSnapshot,
    entries: tuple[tuple[str, Action], ...],
    policy: Policy,
    clock: datetime,
) -> SimulationResult:
    clock = as_utc(clock)
    if clock < snapshot.captured_at:
        raise ValueError("simulation clock cannot predate the captured snapshot")

    batch_block = check_batch(snapshot, tuple(action for _, action in entries), policy)
    issues = {issue.id: issue for issue in snapshot.issues}
    captured_ids = set(issues)
    outcomes: list[ActionOutcome] = []
    comments: list[PredictedComment] = []
    created: list[PredictedIssueLink] = []
    for action_id, action in entries:
        issue_id = referenced_issue_id(action)
        before = issues.get(issue_id) if issue_id is not None and issue_id in captured_ids else None
        reason = batch_block or evaluate_action(
            action, before, snapshot.label_registry, policy, clock
        )
        temporary_id = f"tmp:{action_id}"
        if reason is None and isinstance(action, CreateIssue):
            if len(issues) >= MAX_SNAPSHOT_ISSUES:
                reason = BlockReason.SNAPSHOT_CAPACITY_EXCEEDED
            elif temporary_id in issues:
                reason = BlockReason.TEMPORARY_ID_COLLISION
        if reason is not None:
            # A blocked secret must never be echoed in a simulation report.
            visible_action = None if has_restricted_content(action) else action
            outcomes.append(BlockedAction(action=visible_action, reason_code=reason, before=before))
            continue
        if isinstance(action, CreateIssue):
            after = Issue(
                id=temporary_id,
                board_id=snapshot.board_id,
                title=action.title,
                body=action.body,
                state="open",
                labels=action.labels,
                last_activity_at=clock,
                revision=1,
            )
            created.append(
                PredictedIssueLink(issue_id=temporary_id, source_issue_id=action.source_issue_id)
            )
            issues[after.id] = after
            outcomes.append(
                AllowedAction(
                    action=action, before=None, after=after, changes=_issue_changes(None, after)
                )
            )
            continue
        assert before is not None  # The policy requires a captured issue for these actions.
        if isinstance(action, AddLabel) and action.label in before.labels:
            outcomes.append(NoOpAction(action=action, before=before, after=before))
            continue

        comment = None
        if isinstance(action, CloseIssue):
            after = _close_issue(before, clock)
        elif isinstance(action, AddLabel):
            after = _add_label(before, action)
        else:
            comment = PredictedComment(
                id=temporary_id, issue_id=before.id, body=action.body, created_at=clock
            )
            after = before.model_copy(
                update={"last_activity_at": clock, "revision": before.revision + 1}
            )
            comments.append(comment)
        issues[action.issue_id] = after
        outcomes.append(
            AllowedAction(
                action=action,
                before=before,
                after=after,
                comment=comment,
                changes=_issue_changes(before, after),
            )
        )

    # The source capture metadata is preserved; no persistent board revision was issued.
    predicted = snapshot.model_copy(update={"issues": tuple(issues.values())})
    additions = _PredictedState(
        snapshot=predicted, comments=tuple(comments), created_issues=tuple(created)
    )
    return SimulationResult(
        predicted_snapshot=predicted,
        predicted_fingerprint=(
            fingerprint(additions) if comments or created else predicted.fingerprint()
        ),
        outcomes=tuple(outcomes),
        batch_block=batch_block,
        comments=additions.comments,
        created_issues=additions.created_issues,
    )
