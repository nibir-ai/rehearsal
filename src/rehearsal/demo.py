"""Deterministic demonstration plan shared by the CLI and review page."""

from datetime import timedelta

from rehearsal.domain.actions import Action, AddLabel, CloseIssue, CreateIssue, referenced_issue_id
from rehearsal.domain.models import BoardSnapshot
from rehearsal.domain.plans import (
    ActivityEvidence,
    CandidatePlan,
    Evidence,
    LabelEvidence,
    PlanContext,
    PlanValidationError,
    Proposal,
    ResolutionEvidence,
    validate_plan,
)
from rehearsal.domain.policy import BlockReason, Policy
from rehearsal.domain.simulator import PlanPreview, simulate_plan


def _candidate(snapshot: BoardSnapshot, *, full: bool) -> CandidatePlan:
    actions: tuple[Action, ...] = (
        CloseIssue(issue_id="ISS-104", reason="completed"),
        AddLabel(issue_id="ISS-108", label="needs-follow-up"),
        CloseIssue(issue_id="ISS-112", reason="completed"),
    )
    if full:
        actions = (
            *actions[:2],
            CreateIssue(
                title="Investigate import queue stall",
                body="Follow-up for ISS-108",
                labels=("follow-up",),
                source_issue_id="ISS-108",
            ),
            actions[2],
            CloseIssue(issue_id="ISS-117", reason="completed"),
        )
    issues = {issue.id: issue for issue in snapshot.issues}
    proposals: list[Proposal] = []
    for index, action in enumerate(actions, start=1):
        issue_id = referenced_issue_id(action)
        issue = issues.get(issue_id) if issue_id is not None else None
        if issue is None:
            raise PlanValidationError(BlockReason.ISSUE_NOT_FOUND, issue_id or "fixture source")
        labels = LabelEvidence(issue_id=issue.id, field="labels", expected_value=issue.labels)
        evidence: tuple[Evidence, ...]
        if isinstance(action, CloseIssue):
            evidence = (
                labels,
                ActivityEvidence(
                    issue_id=issue.id,
                    field="last_activity_at",
                    expected_value=issue.last_activity_at,
                ),
                ResolutionEvidence(
                    issue_id=issue.id,
                    field="resolved_by_maintainer",
                    expected_value=issue.resolved_by_maintainer,
                ),
            )
            rationale = "Check closure against captured activity, resolution, and labels."
        else:
            evidence = (labels,)
            rationale = "Show follow-up work on the blocked issue."
        proposals.append(
            Proposal(action_id=f"a{index}", action=action, evidence=evidence, rationale=rationale)
        )
    return CandidatePlan(
        goal_summary="Rehearse stale-issue maintenance", proposals=tuple(proposals)
    )


def preview_demo(snapshot: BoardSnapshot, *, full: bool = False) -> PlanPreview:
    clock = snapshot.captured_at
    source_fingerprint = snapshot.fingerprint()
    policy = Policy()
    plan = validate_plan(
        _candidate(snapshot, full=full),
        snapshot,
        policy,
        PlanContext(
            run_id="fixture-platform-demo",
            plan_version=1,
            created_at=clock,
            expires_at=clock + timedelta(minutes=10),
            simulation_clock=clock,
        ),
    )
    preview = simulate_plan(snapshot, plan, policy)
    if source_fingerprint != snapshot.fingerprint():
        raise RuntimeError("simulation changed the source snapshot")
    return preview
