"""Apply an authorized IssueHub plan and compare its effects with a later read."""

import json
import re
import sqlite3
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime

from rehearsal.domain.actions import ActionKind, AddComment, AddLabel, CloseIssue, CreateIssue
from rehearsal.domain.canonical import canonical_json
from rehearsal.domain.models import FrozenModel, Issue, Revision, as_utc, canonical_labels
from rehearsal.domain.plans import Sha256
from rehearsal.domain.simulator import IssueFieldValue, PlanPreview

ReceiptStatus = Literal["APPLIED_UNVERIFIED", "VERIFIED", "DIVERGED", "UNVERIFIED"]
SIMULATOR_VERSION = "sim-v1"
_NUMERIC_ISSUE_ID = re.compile(r"ISS-([0-9]+)")


class OperationReceipt(FrozenModel):
    action_id: str
    kind: ActionKind
    target: str
    temporary_id: str | None = None
    comment_id: int | None = None
    attempted: Literal[True] = True
    predicted: dict[str, IssueFieldValue]
    observed: dict[str, IssueFieldValue] | None = None
    verification: Literal["PENDING", "MATCH", "MISMATCH"] = "PENDING"


class ExecutionReceipt(FrozenModel):
    run_id: str
    approval_id: str
    plan_digest: Sha256
    target_id: str
    policy_version: str
    simulator_version: Literal["sim-v1"] = SIMULATOR_VERSION
    status: ReceiptStatus = "APPLIED_UNVERIFIED"
    attempted_at: AwareDatetime
    expected_board_revision: Revision
    observed_board_revision: Revision | None = None
    verified_at: AwareDatetime | None = None
    operations: tuple[OperationReceipt, ...]
    uncertainties: tuple[str, ...] = ()


def _timestamp(clock: datetime) -> str:
    return as_utc(clock).isoformat().replace("+00:00", "Z")


def _expected(issue: Issue) -> dict[str, IssueFieldValue]:
    return {
        "state": issue.state,
        "labels": issue.labels,
        "revision": issue.revision,
        "last_activity_at": _timestamp(issue.last_activity_at),
    }


def _next_issue_number(db: sqlite3.Connection, board_id: str, stored: int) -> int:
    numbers = (
        int(match.group(1))
        for row in db.execute("SELECT id FROM issues WHERE board_id = ?", (board_id,))
        if (match := _NUMERIC_ISSUE_ID.fullmatch(row["id"]))
    )
    return max(stored, max(numbers, default=0) + 1)


def apply_plan(
    db: sqlite3.Connection,
    preview: PlanPreview,
    *,
    approval_id: str,
    actor_id: str,
    now: datetime,
) -> ExecutionReceipt:
    """Apply already checked actions using the caller's write transaction."""
    plan = preview.plan
    board_id = plan.target_id
    final_issues = {issue.id: issue for issue in preview.simulation.predicted_snapshot.issues}
    board = db.execute(
        "SELECT revision, next_issue_number FROM boards WHERE id = ?", (board_id,)
    ).fetchone()
    if board is None or board["revision"] != plan.board_revision:
        raise sqlite3.IntegrityError("board revision changed")
    outcomes = preview.simulation.allowed
    if len(outcomes) != len(plan.actions):
        raise sqlite3.IntegrityError("plan contains non-effectful actions")
    next_number = board["next_issue_number"]
    allocating = any(isinstance(entry.action, CreateIssue) for entry in plan.actions)
    if allocating:
        next_number = _next_issue_number(db, board_id, next_number)
    operations: list[OperationReceipt] = []
    activity = _timestamp(plan.simulation_clock)

    for ordinal, (entry, outcome) in enumerate(zip(plan.actions, outcomes, strict=True)):
        action = entry.action
        if action != outcome.action:
            raise sqlite3.IntegrityError("action differs from approved plan")
        expected = _expected(final_issues[outcome.after.id])
        temporary_id = None
        comment_id = None
        if isinstance(action, CreateIssue):
            target = f"ISS-{next_number}"
            next_number += 1
            temporary_id = outcome.after.id
            expected.update(
                title=action.title, body=action.body, source_issue_id=action.source_issue_id
            )
        else:
            target = action.issue_id
            if isinstance(action, CloseIssue):
                expected["close_reason"] = action.reason
            elif isinstance(action, AddComment):
                comment_id = db.execute(
                    """SELECT COALESCE(MAX(id), 0) + 1 FROM comments
                       WHERE board_id = ? AND issue_id = ?""",
                    (board_id, target),
                ).fetchone()[0]
                if outcome.comment is None:
                    raise sqlite3.IntegrityError("comment prediction missing")
                temporary_id = outcome.comment.id
                expected.update(
                    comment_body=outcome.comment.body,
                    comment_author=actor_id,
                    comment_created_at=_timestamp(outcome.comment.created_at),
                )
        operation = OperationReceipt(
            action_id=entry.action_id,
            kind=action.kind,
            target=target,
            temporary_id=temporary_id,
            comment_id=comment_id,
            predicted=expected,
        )
        db.execute(
            "INSERT INTO operation_journal VALUES (?, ?, ?, ?)",
            (plan.run_id, entry.action_id, ordinal, canonical_json(operation).decode()),
        )
        if isinstance(action, CreateIssue):
            db.execute(
                """INSERT INTO issues (
                       board_id, id, title, body, state, labels_json, last_activity_at,
                       revision, resolved_by_maintainer, source_issue_id
                   ) VALUES (?, ?, ?, ?, 'open', ?, ?, 1, 0, ?)""",
                (
                    board_id,
                    target,
                    action.title,
                    action.body,
                    json.dumps(action.labels),
                    activity,
                    action.source_issue_id,
                ),
            )
        else:
            _apply_to_issue(db, board_id, target, action, activity, actor_id, comment_id)
        operations.append(operation)

    revision = plan.board_revision + len(operations)
    db.execute(
        "UPDATE boards SET revision = ?, next_issue_number = ? WHERE id = ?",
        (revision, next_number, board_id),
    )
    return ExecutionReceipt(
        run_id=plan.run_id,
        approval_id=approval_id,
        plan_digest=preview.plan_digest,
        target_id=board_id,
        policy_version=plan.policy_version,
        attempted_at=now,
        expected_board_revision=revision,
        operations=tuple(operations),
        uncertainties=("READBACK_PENDING",),
    )


def _apply_to_issue(
    db: sqlite3.Connection,
    board_id: str,
    target: str,
    action: AddLabel | AddComment | CloseIssue,
    activity: str,
    actor_id: str,
    comment_id: int | None,
) -> None:
    issue_key = (board_id, target)
    if isinstance(action, CloseIssue):
        changed = db.execute(
            """UPDATE issues SET state = 'closed', close_reason = ?, last_activity_at = ?,
                      revision = revision + 1 WHERE board_id = ? AND id = ? AND state = 'open'""",
            (action.reason, activity, *issue_key),
        )
    elif isinstance(action, AddLabel):
        row = db.execute(
            "SELECT labels_json FROM issues WHERE board_id = ? AND id = ?", issue_key
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("label target missing")
        labels = canonical_labels((*json.loads(row["labels_json"]), action.label))
        changed = db.execute(
            """UPDATE issues SET labels_json = ?, revision = revision + 1
               WHERE board_id = ? AND id = ?""",
            (json.dumps(labels), *issue_key),
        )
    else:
        db.execute(
            "INSERT INTO comments VALUES (?, ?, ?, ?, ?, ?)",
            (*issue_key, comment_id, actor_id, action.body, activity),
        )
        changed = db.execute(
            """UPDATE issues SET last_activity_at = ?, revision = revision + 1
               WHERE board_id = ? AND id = ?""",
            (activity, *issue_key),
        )
    if changed.rowcount != 1:
        raise sqlite3.IntegrityError("issue update did not affect the approved target")


def verify_receipt(
    db: sqlite3.Connection, receipt: ExecutionReceipt, *, now: datetime
) -> ExecutionReceipt:
    """Read back one consistent committed revision and compare affected records."""
    board = db.execute("SELECT revision FROM boards WHERE id = ?", (receipt.target_id,)).fetchone()
    revision = board["revision"] if board is not None else None
    operations = tuple(_verify_operation(db, receipt.target_id, op) for op in receipt.operations)
    matches = revision == receipt.expected_board_revision and all(
        op.verification == "MATCH" for op in operations
    )
    return receipt.model_copy(
        update={
            "status": "VERIFIED" if matches else "DIVERGED",
            "operations": operations,
            "observed_board_revision": revision,
            "verified_at": now,
            "uncertainties": (
                () if revision == receipt.expected_board_revision else ("BOARD_REVISION_MISMATCH",)
            ),
        }
    )


def _verify_operation(
    db: sqlite3.Connection, board_id: str, operation: OperationReceipt
) -> OperationReceipt:
    issue_key = (board_id, operation.target)
    row = db.execute("SELECT * FROM issues WHERE board_id = ? AND id = ?", issue_key).fetchone()
    observed: dict[str, IssueFieldValue] | None = None
    if row is not None:
        observed = {
            "state": row["state"],
            "labels": canonical_labels(tuple(json.loads(row["labels_json"]))),
            "revision": row["revision"],
            "last_activity_at": _timestamp(datetime.fromisoformat(row["last_activity_at"])),
        }
        if operation.kind == "create_issue":
            observed.update(
                title=row["title"], body=row["body"], source_issue_id=row["source_issue_id"]
            )
        elif operation.kind == "close_issue":
            observed["close_reason"] = row["close_reason"]
        elif operation.kind == "add_comment":
            comment = db.execute(
                """SELECT body, author, created_at FROM comments
                   WHERE board_id = ? AND issue_id = ? AND id = ?""",
                (*issue_key, operation.comment_id),
            ).fetchone()
            if comment is not None:
                observed.update(
                    comment_body=comment["body"],
                    comment_author=comment["author"],
                    comment_created_at=_timestamp(datetime.fromisoformat(comment["created_at"])),
                )
    return operation.model_copy(
        update={
            "observed": observed,
            "verification": "MATCH" if observed == operation.predicted else "MISMATCH",
        }
    )
