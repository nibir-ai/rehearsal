"""Read the local fixture and print its deterministic rehearsal."""

import argparse
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

from rehearsal.domain.actions import Action, AddLabel, CloseIssue
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
from rehearsal.domain.simulator import BlockedAction, NoOpAction, simulate_plan


def _describe(action: Action) -> str:
    if isinstance(action, CloseIssue):
        return f"close {action.issue_id}"
    return f"add {action.label} to {action.issue_id}"


def _candidate(snapshot: BoardSnapshot) -> CandidatePlan:
    actions = (
        CloseIssue(issue_id="ISS-104", reason="completed"),
        AddLabel(issue_id="ISS-108", label="needs-follow-up"),
        CloseIssue(issue_id="ISS-112", reason="completed"),
    )
    issues = {issue.id: issue for issue in snapshot.issues}
    proposals: list[Proposal] = []
    for index, action in enumerate(actions, start=1):
        issue = issues.get(action.issue_id)
        if issue is None:
            raise PlanValidationError(BlockReason.ISSUE_NOT_FOUND, action.issue_id)
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


def _demo(snapshot: BoardSnapshot, *, json_output: bool) -> None:
    clock = snapshot.captured_at
    source_fingerprint = snapshot.fingerprint()
    policy = Policy()
    plan = validate_plan(
        _candidate(snapshot),
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

    if json_output:
        print(preview.model_dump_json(indent=2))
        return

    print("REHEARSAL v0.1 / deterministic demo")
    print(
        f"Target: {snapshot.board_id} | revision: {snapshot.board_revision} | "
        f"clock: {clock.isoformat().replace('+00:00', 'Z')}"
    )
    print("Candidate: " + "; ".join(_describe(entry.action) for entry in plan.actions))
    print(f"Preview plan digest: {preview.plan_digest}")
    print("Simulation (before -> predicted after):")
    for outcome in preview.simulation.outcomes:
        action = outcome.action
        kind = "close" if isinstance(action, CloseIssue) else "add_label"
        if isinstance(outcome, BlockedAction):
            detail = f"{outcome.reason_code}; no change"
        elif isinstance(outcome, NoOpAction):
            detail = f"{outcome.action.label} already present; no change"
        elif isinstance(action, CloseIssue):
            detail = f"{outcome.before.state} -> {outcome.after.state}"
        else:
            before = ", ".join(outcome.before.labels) or "(none)"
            after = ", ".join(outcome.after.labels)
            detail = f"add {action.label}; labels: {before} -> {after}"
        print(f"  {outcome.status} {kind} {action.issue_id}  [{detail}]")
    print("SOURCE SNAPSHOT: unchanged (SHA-256 match)")
    print("LIVE WRITE COUNT: 0")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="rehearse the IssueHub fixture actions")
    demo.add_argument("--fixture", required=True, type=Path, help="path to the board snapshot JSON")
    demo.add_argument("--json", action="store_true", help="print the exact plan and simulation")
    args = parser.parse_args(argv)
    try:
        snapshot = BoardSnapshot.model_validate_json(args.fixture.read_bytes())
    except (OSError, ValidationError) as exc:
        parser.error(f"could not load fixture: {exc}")
    try:
        _demo(snapshot, json_output=args.json)
    except (PlanValidationError, ValidationError) as exc:
        parser.error(f"could not rehearse fixture: {exc}")


if __name__ == "__main__":
    main()
