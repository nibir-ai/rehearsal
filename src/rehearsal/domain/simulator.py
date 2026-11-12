"""Pure IssueHub simulation: no adapters, I/O, or ambient clock."""

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from rehearsal.domain.actions import Action, AddLabel, CloseIssue
from rehearsal.domain.models import BoardSnapshot, FrozenModel, Issue, as_utc, canonical_labels
from rehearsal.domain.plans import Sha256, ValidatedPlan, check_plan_context
from rehearsal.domain.policy import BlockReason, Policy, check_batch, evaluate_action


class AllowedAction(FrozenModel):
    status: Literal["ALLOWED"] = "ALLOWED"
    action: Action
    before: Issue
    after: Issue


class BlockedAction(FrozenModel):
    status: Literal["BLOCKED"] = "BLOCKED"
    action: Action
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


def simulate_plan(snapshot: BoardSnapshot, plan: ValidatedPlan, policy: Policy) -> PlanPreview:
    """Preview the exact, validated plan against the inputs bound into its digest."""
    check_plan_context(plan, snapshot, policy)
    return PlanPreview(
        plan=plan,
        plan_digest=plan.digest(),
        simulation=simulate(
            snapshot,
            tuple(entry.action for entry in plan.actions),
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
    clock = as_utc(clock)
    if clock < snapshot.captured_at:
        raise ValueError("simulation clock cannot predate the captured snapshot")

    batch = tuple(actions)
    batch_block = check_batch(snapshot, len(batch), policy)
    issues = {issue.id: issue for issue in snapshot.issues}
    outcomes: list[ActionOutcome] = []
    for action in batch:
        before = issues.get(action.issue_id)
        if batch_block is not None:
            outcomes.append(BlockedAction(action=action, reason_code=batch_block, before=before))
            continue
        if before is None:
            outcomes.append(
                BlockedAction(action=action, reason_code=BlockReason.ISSUE_NOT_FOUND, before=None)
            )
            continue
        reason = evaluate_action(action, before, snapshot.label_registry, policy, clock)
        if reason is not None:
            outcomes.append(BlockedAction(action=action, reason_code=reason, before=before))
            continue
        if isinstance(action, AddLabel) and action.label in before.labels:
            outcomes.append(NoOpAction(action=action, before=before, after=before))
            continue

        after = (
            _close_issue(before, clock)
            if isinstance(action, CloseIssue)
            else _add_label(before, action)
        )
        issues[action.issue_id] = after
        outcomes.append(AllowedAction(action=action, before=before, after=after))

    # The source capture metadata is preserved; no persistent board revision was issued.
    predicted = snapshot.model_copy(update={"issues": tuple(issues.values())})
    return SimulationResult(
        predicted_snapshot=predicted,
        predicted_fingerprint=predicted.fingerprint(),
        outcomes=tuple(outcomes),
        batch_block=batch_block,
    )
