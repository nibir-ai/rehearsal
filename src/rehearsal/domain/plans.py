"""Validate untrusted proposals and bind plans to trusted preview inputs."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from rehearsal.domain.actions import Action, AddLabel
from rehearsal.domain.canonical import fingerprint
from rehearsal.domain.models import (
    BoardSnapshot,
    FrozenModel,
    IssueId,
    Label,
    Revision,
    as_utc,
    canonical_labels,
)
from rehearsal.domain.policy import MAX_ACTIONS, BlockReason, Policy, check_batch

Identifier = Annotated[str, Field(min_length=1, max_length=64)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ACTION_SCHEMA_VERSION = "issuehub-actions-v1"
PLAN_DIGEST_PREFIX = b"rehearsal-plan-v1\n"


class StateEvidence(FrozenModel):
    issue_id: IssueId
    field: Literal["state"]
    expected_value: Literal["open", "closed"]


class LabelEvidence(FrozenModel):
    issue_id: IssueId
    field: Literal["labels"]
    expected_value: tuple[Label, ...]

    _normalize = field_validator("expected_value")(canonical_labels)


class ActivityEvidence(FrozenModel):
    issue_id: IssueId
    field: Literal["last_activity_at"]
    expected_value: AwareDatetime

    _normalize = field_validator("expected_value")(as_utc)


class ResolutionEvidence(FrozenModel):
    issue_id: IssueId
    field: Literal["resolved_by_maintainer"]
    expected_value: bool


Evidence = Annotated[
    StateEvidence | LabelEvidence | ActivityEvidence | ResolutionEvidence,
    Field(discriminator="field"),
]


class Proposal(FrozenModel):
    action_id: Identifier
    action: Action
    evidence: tuple[Evidence, ...] = Field(min_length=1, max_length=4)
    rationale: str = Field(min_length=1, max_length=500)


class CandidatePlan(FrozenModel):
    goal_summary: str = Field(min_length=1, max_length=500)
    proposals: tuple[Proposal, ...] = Field(min_length=1, max_length=MAX_ACTIONS)


class PlanContext(FrozenModel):
    """Server-supplied metadata. Fixture mode needs no MCP connection or owner session."""

    run_id: Identifier
    plan_version: Revision
    created_at: AwareDatetime
    expires_at: AwareDatetime
    simulation_clock: AwareDatetime
    proposer_type: Literal["fixture"] = "fixture"
    owner_id: Identifier | None = None
    mcp_contract_hash: Sha256 | None = None

    _normalize_clocks = field_validator("created_at", "expires_at", "simulation_clock")(as_utc)

    @model_validator(mode="after")
    def check_lifetime(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("plan expiry must be later than creation")
        return self


class BoundAction(Proposal):
    observed_issue_revision: Revision


class ValidatedPlan(PlanContext):
    """An exact proposal with checked evidence; policy outcomes still require simulation."""

    target_id: Identifier
    snapshot_id: Identifier
    board_revision: Revision
    snapshot_fingerprint: Sha256
    policy_id: Identifier
    policy_version: Identifier
    policy_fingerprint: Sha256
    action_schema_version: Literal["issuehub-actions-v1"]
    goal_summary: str = Field(min_length=1, max_length=500)
    actions: tuple[BoundAction, ...] = Field(min_length=1, max_length=MAX_ACTIONS)

    def digest(self) -> str:
        return fingerprint(self, prefix=PLAN_DIGEST_PREFIX)


class PlanErrorCode(StrEnum):
    DUPLICATE_ACTION = "DUPLICATE_ACTION"
    DUPLICATE_ACTION_ID = "DUPLICATE_ACTION_ID"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"
    SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"
    POLICY_CHANGED = "POLICY_CHANGED"
    INVALID_CLOCK = "INVALID_CLOCK"
    SCHEMA_INVALID = "SCHEMA_INVALID"


class PlanValidationError(ValueError):
    def __init__(self, code: PlanErrorCode | BlockReason, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _bind_actions(
    proposals: tuple[Proposal, ...], snapshot: BoardSnapshot, policy: Policy
) -> tuple[BoundAction, ...]:
    if block := check_batch(snapshot, len(proposals), policy):
        raise PlanValidationError(block, "batch exceeds trusted policy scope")

    issues = {issue.id: issue for issue in snapshot.issues}
    action_ids: set[str] = set()
    operations: set[tuple[str, str, str | None]] = set()
    bound: list[BoundAction] = []
    for proposal in proposals:
        action = proposal.action
        operation = (
            action.kind,
            action.issue_id,
            action.label if isinstance(action, AddLabel) else None,
        )
        if proposal.action_id in action_ids:
            raise PlanValidationError(PlanErrorCode.DUPLICATE_ACTION_ID, proposal.action_id)
        if operation in operations:
            raise PlanValidationError(PlanErrorCode.DUPLICATE_ACTION, proposal.action_id)
        action_ids.add(proposal.action_id)
        operations.add(operation)

        issue = issues.get(action.issue_id)
        if issue is None:
            raise PlanValidationError(BlockReason.ISSUE_NOT_FOUND, action.issue_id)
        evidence_fields = set()
        for evidence in proposal.evidence:
            if (
                evidence.issue_id != issue.id
                or evidence.field in evidence_fields
                or evidence.expected_value != getattr(issue, evidence.field)
            ):
                raise PlanValidationError(PlanErrorCode.INVALID_EVIDENCE, proposal.action_id)
            evidence_fields.add(evidence.field)

        bound.append(
            BoundAction(
                action_id=proposal.action_id,
                action=action,
                evidence=proposal.evidence,
                rationale=proposal.rationale,
                observed_issue_revision=issue.revision,
            )
        )
    return tuple(bound)


def validate_plan(
    candidate: CandidatePlan, snapshot: BoardSnapshot, policy: Policy, context: PlanContext
) -> ValidatedPlan:
    """Build the canonical plan from the candidate and independent trusted inputs."""
    if min(context.created_at, context.simulation_clock) < snapshot.captured_at:
        raise PlanValidationError(PlanErrorCode.INVALID_CLOCK, "plan predates the capture")
    return ValidatedPlan(
        **context.model_dump(),
        target_id=snapshot.board_id,
        snapshot_id=snapshot.snapshot_id,
        board_revision=snapshot.board_revision,
        snapshot_fingerprint=snapshot.fingerprint(),
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_fingerprint=fingerprint(policy),
        action_schema_version=ACTION_SCHEMA_VERSION,
        goal_summary=candidate.goal_summary,
        actions=_bind_actions(candidate.proposals, snapshot, policy),
    )


def check_plan_context(plan: ValidatedPlan, snapshot: BoardSnapshot, policy: Policy) -> None:
    if plan.action_schema_version != ACTION_SCHEMA_VERSION:
        raise PlanValidationError(PlanErrorCode.SCHEMA_INVALID, "unknown action schema version")
    if (plan.target_id, plan.snapshot_id, plan.board_revision) != (
        snapshot.board_id,
        snapshot.snapshot_id,
        snapshot.board_revision,
    ) or plan.snapshot_fingerprint != snapshot.fingerprint():
        raise PlanValidationError(PlanErrorCode.SNAPSHOT_MISMATCH, "capture differs from plan")
    if (plan.policy_id, plan.policy_version) != (
        policy.policy_id,
        policy.policy_version,
    ) or plan.policy_fingerprint != fingerprint(policy):
        raise PlanValidationError(PlanErrorCode.POLICY_CHANGED, "policy differs from plan")
    if min(plan.created_at, plan.simulation_clock) < snapshot.captured_at:
        raise PlanValidationError(PlanErrorCode.INVALID_CLOCK, "plan predates the capture")
    if plan.actions != _bind_actions(plan.actions, snapshot, policy):
        raise PlanValidationError(PlanErrorCode.SNAPSHOT_MISMATCH, "issue revisions differ")
