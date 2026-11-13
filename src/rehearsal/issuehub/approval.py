"""Persist exact owner-reviewed plans and record explicit decisions."""

import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import digest as hmac_digest
from pathlib import Path
from secrets import compare_digest, token_hex, token_urlsafe
from typing import Literal

from pydantic import AwareDatetime

from rehearsal.demo import DEMO_GOAL, demo_candidate
from rehearsal.domain.canonical import canonical_json
from rehearsal.domain.models import BoardSnapshot, FrozenModel, as_utc
from rehearsal.domain.plans import (
    CandidatePlan,
    PlanContext,
    PlanErrorCode,
    PlanValidationError,
    Sha256,
    ValidatedPlan,
    validate_plan,
)
from rehearsal.domain.policy import Policy
from rehearsal.domain.simulator import AllowedAction, PlanPreview, simulate_plan
from rehearsal.issuehub.execution import ExecutionReceipt, ReceiptStatus, apply_plan, verify_receipt
from rehearsal.issuehub.reader import BoardNotFound, IssueHubReader, capture_board
from rehearsal.issuehub.review import ReviewReport, review_report
from rehearsal.issuehub.schema import SCHEMA_VERSION, schema_version
from rehearsal.model_planner import ModelPlannerError, OllamaPlanner

OWNER_ID = "local-owner"
SESSION_TTL = timedelta(hours=8)
PLAN_TTL = timedelta(minutes=10)
RunStatus = Literal["AWAITING_APPROVAL", "APPROVED", "REJECTED", "STALE"] | ReceiptStatus
_NONCE = re.compile(r"[A-Za-z0-9_-]{43}")


class ApprovalError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OwnerSession(FrozenModel):
    token: str
    csrf_token: str
    expires_at: AwareDatetime


class RunReview(FrozenModel):
    run_id: str
    status: RunStatus
    expires_at: AwareDatetime
    review: ReviewReport


class ApprovalRecord(FrozenModel):
    approval_id: str
    run_id: str
    plan_digest: Sha256
    target_id: str
    snapshot_revision: int
    policy_version: str
    actor_id: str
    approved_at: AwareDatetime
    expires_at: AwareDatetime
    status: Literal["APPROVED"] = "APPROVED"


class Rejection(FrozenModel):
    run_id: str
    status: Literal["REJECTED"] = "REJECTED"


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ApprovalService:
    def __init__(
        self,
        database: Path,
        *,
        owner_secret: str,
        policy: Policy | None = None,
        clock: Callable[[], datetime] = _utc_now,
        planner: Callable[[BoardSnapshot], CandidatePlan] | None = None,
        model_planner: OllamaPlanner | None = None,
    ) -> None:
        if len(owner_secret) < 32 or not owner_secret.isascii() or not owner_secret.isprintable():
            raise ValueError("owner secret must be at least 32 printable ASCII characters")
        if any(character.isspace() for character in owner_secret):
            raise ValueError("owner secret must not contain whitespace")
        self._database = database.resolve().as_uri()
        self._secret_hash = _hash(owner_secret)
        self._policy = policy if policy is not None else Policy()
        self._reader = IssueHubReader(database, allowed_board_id=self._policy.allowed_board_id)
        self._clock = clock
        self._planner = planner
        self._model_planner = model_planner

    def _session_hash(self, token: str) -> str:
        return hmac_digest(bytes.fromhex(self._secret_hash), token.encode(), "sha256").hex()

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        mode = "rw" if write else "ro"
        with closing(sqlite3.connect(f"{self._database}?mode={mode}", uri=True)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if schema_version(db) != SCHEMA_VERSION:
                raise ApprovalError("MIGRATION_REQUIRED")
            yield db

    def open_session(self, secret: str) -> OwnerSession:
        if not compare_digest(_hash(secret), self._secret_hash):
            raise ApprovalError("OWNER_AUTH_REQUIRED")
        with self._transaction(write=True) as db:
            session = OwnerSession(
                token=token_urlsafe(32),
                csrf_token=token_urlsafe(32),
                expires_at=as_utc(self._clock()) + SESSION_TTL,
            )
            db.execute(
                "INSERT INTO owner_sessions VALUES (?, ?, ?)",
                (
                    self._session_hash(session.token),
                    _hash(session.csrf_token),
                    session.expires_at.isoformat(),
                ),
            )
        return session

    def _session(
        self,
        db: sqlite3.Connection,
        token: str,
        now: datetime,
        *,
        csrf: str | None = None,
    ) -> tuple[str, datetime]:
        if not token:
            raise ApprovalError("OWNER_AUTH_REQUIRED")
        session_hash = self._session_hash(token)
        session = db.execute(
            "SELECT csrf_hash, expires_at FROM owner_sessions WHERE id_hash = ?", (session_hash,)
        ).fetchone()
        if session is None:
            raise ApprovalError("OWNER_AUTH_REQUIRED")
        expires_at = datetime.fromisoformat(session["expires_at"])
        if now >= expires_at:
            raise ApprovalError("OWNER_AUTH_REQUIRED")
        if csrf is not None and not compare_digest(_hash(csrf), session["csrf_hash"]):
            raise ApprovalError("CSRF_INVALID")
        return session_hash, expires_at

    def create_run(
        self,
        target_id: str,
        token: str,
        csrf: str,
        *,
        goal: str = DEMO_GOAL,
        use_model: bool = False,
    ) -> RunReview:
        with self._transaction(write=False) as db:
            now = as_utc(self._clock())
            session_hash, session_expiry = self._session(db, token, now, csrf=csrf)
            if target_id != self._policy.allowed_board_id:
                raise ApprovalError("TARGET_OUT_OF_SCOPE")
        if not use_model and goal != DEMO_GOAL:
            raise ApprovalError("DEMO_GOAL_REQUIRED")
        model = self._model_planner if use_model else None
        if use_model and model is None:
            raise ApprovalError("MODEL_DISABLED")

        try:
            snapshot = self._reader.capture(target_id, clock=now)
        except BoardNotFound as exc:
            raise ApprovalError("BOARD_NOT_FOUND") from exc
        try:
            if model is not None:
                proposal = model.propose(goal, snapshot)
            elif self._planner is not None:
                proposal = self._planner(snapshot)
            else:
                proposal = demo_candidate(snapshot, full=True)
        except ModelPlannerError as exc:
            raise ApprovalError(str(exc)) from None
        except PlanValidationError as exc:
            raise ApprovalError("DEMO_PLAN_UNAVAILABLE") from exc
        if not proposal.proposals:
            raise ApprovalError("NO_APPROVABLE_ACTIONS")

        now = as_utc(self._clock())
        if now >= session_expiry:
            raise ApprovalError("OWNER_AUTH_REQUIRED")
        run_id = token_hex(16)
        context = PlanContext(
            run_id=run_id,
            plan_version=1,
            created_at=now,
            expires_at=min(now + PLAN_TTL, session_expiry),
            simulation_clock=snapshot.captured_at,
            owner_id=OWNER_ID,
            proposer_type="model" if model is not None else "fixture",
            mcp_contract_hash=model.contract_hash if model is not None else None,
        )
        try:
            candidate_plan = validate_plan(proposal, snapshot, self._policy, context)
            candidate_preview = simulate_plan(snapshot, candidate_plan, self._policy)
            allowed = tuple(
                entry
                for entry, outcome in zip(
                    proposal.proposals, candidate_preview.simulation.outcomes, strict=True
                )
                if isinstance(outcome, AllowedAction)
            )
            if not allowed:
                raise ApprovalError("NO_APPROVABLE_ACTIONS")
            exact = CandidatePlan(goal_summary=proposal.goal_summary, proposals=allowed)
            plan = validate_plan(exact, snapshot, self._policy, context)
            preview = self._exact_preview(snapshot, plan)
        except PlanValidationError as exc:
            code = "MODEL_PROPOSAL_REJECTED" if model is not None else "DEMO_PLAN_UNAVAILABLE"
            raise ApprovalError(code) from exc
        with self._transaction(write=True) as db:
            now = as_utc(self._clock())
            self._session(db, token, now, csrf=csrf)
            if self._fresh_snapshot(db, plan) is None:
                raise ApprovalError("SNAPSHOT_STALE")
            if now >= plan.expires_at:
                raise ApprovalError("APPROVAL_EXPIRED")
            db.execute(
                """INSERT INTO runs VALUES (?, ?, ?, 'AWAITING_APPROVAL', ?, ?)""",
                (run_id, session_hash, target_id, now.isoformat(), now.isoformat()),
            )
            db.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?)",
                (run_id, canonical_json(snapshot).decode(), snapshot.fingerprint()),
            )
            db.execute(
                "INSERT INTO plans VALUES (?, ?, ?)",
                (run_id, canonical_json(plan).decode(), plan.digest()),
            )
            return self._review("AWAITING_APPROVAL", preview)

    def get_review(self, run_id: str, token: str) -> RunReview:
        with self._transaction(write=False) as db:
            session_hash, _ = self._session(db, token, as_utc(self._clock()))
            status, snapshot, plan = self._load(db, run_id, session_hash)
            return self._review(status, self._exact_preview(snapshot, plan))

    def approve(self, run_id: str, digest: str, token: str, csrf: str) -> ApprovalRecord:
        with self._transaction(write=True) as db:
            now = as_utc(self._clock())
            session_hash, session_expiry = self._session(db, token, now, csrf=csrf)
            status, snapshot, plan = self._load(db, run_id, session_hash)
            self._pending(status, plan, digest, now)
            self._exact_preview(snapshot, plan)
            if self._fresh_snapshot(db, plan) is None:
                self._transition(db, run_id, "STALE", now)
            else:
                record = ApprovalRecord(
                    approval_id=token_hex(16),
                    run_id=run_id,
                    plan_digest=plan.digest(),
                    target_id=plan.target_id,
                    snapshot_revision=plan.board_revision,
                    policy_version=plan.policy_version,
                    actor_id=OWNER_ID,
                    approved_at=now,
                    expires_at=min(plan.expires_at, session_expiry),
                )
                db.execute(
                    "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (
                        record.approval_id,
                        run_id,
                        record.plan_digest,
                        record.target_id,
                        record.snapshot_revision,
                        record.policy_version,
                        plan.mcp_contract_hash,
                        OWNER_ID,
                        session_hash,
                        token_urlsafe(32),
                        now.isoformat(),
                        record.expires_at.isoformat(),
                    ),
                )
                self._transition(db, run_id, "APPROVED", now)
                return record
        raise ApprovalError("SNAPSHOT_STALE")

    def reject(self, run_id: str, digest: str, token: str, csrf: str) -> Rejection:
        with self._transaction(write=True) as db:
            now = as_utc(self._clock())
            session_hash, _ = self._session(db, token, now, csrf=csrf)
            status, _, plan = self._load(db, run_id, session_hash)
            self._pending(status, plan, digest, now)
            self._transition(db, run_id, "REJECTED", now)
        return Rejection(run_id=run_id)

    def execute(self, run_id: str, token: str, csrf: str) -> ExecutionReceipt:
        """Consume approval and apply the exact plan, then verify after commit."""
        with self._transaction(write=True) as db:
            now = as_utc(self._clock())
            session_hash, session_expiry = self._session(db, token, now, csrf=csrf)
            status, snapshot, plan = self._load(db, run_id, session_hash)
            existing = self._get_receipt(db, run_id, session_hash)
            if existing is not None:
                return existing
            if status != "APPROVED":
                raise ApprovalError("RUN_NOT_APPROVED")
            approval = db.execute("SELECT * FROM approvals WHERE run_id = ?", (run_id,)).fetchone()
            if approval is None or (
                approval["actor_id"] != OWNER_ID
                or approval["session_hash"] != session_hash
                or approval["plan_digest"] != plan.digest()
                or approval["target_id"] != plan.target_id
                or approval["snapshot_revision"] != plan.board_revision
                or approval["policy_version"] != plan.policy_version
                or approval["tool_contract_hash"] != plan.mcp_contract_hash
                or _NONCE.fullmatch(approval["nonce"]) is None
            ):
                raise ApprovalError("APPROVAL_INVALID")
            if approval["consumed_at"] is not None:
                raise ApprovalError("APPROVAL_REPLAY")
            approved_at = datetime.fromisoformat(approval["approved_at"])
            expires_at = datetime.fromisoformat(approval["expires_at"])
            if (
                not plan.created_at
                <= approved_at
                <= now
                < expires_at
                <= min(plan.expires_at, session_expiry)
            ):
                raise ApprovalError("APPROVAL_EXPIRED")
            self._exact_preview(snapshot, plan)
            current = self._fresh_snapshot(db, plan)
            if current is None:
                self._transition(db, run_id, "STALE", now, expected="APPROVED")
            else:
                preview = self._exact_preview(current, plan)
                receipt = apply_plan(
                    db, preview, approval_id=approval["id"], actor_id=OWNER_ID, now=now
                )
                consumed = db.execute(
                    """UPDATE approvals SET consumed_at = ?
                       WHERE id = ? AND nonce = ? AND consumed_at IS NULL""",
                    (now.isoformat(), approval["id"], approval["nonce"]),
                )
                if consumed.rowcount != 1:
                    raise ApprovalError("APPROVAL_REPLAY")
                db.execute(
                    "INSERT INTO receipts VALUES (?, ?, ?)",
                    (run_id, receipt.status, canonical_json(receipt).decode()),
                )
        if current is None:
            raise ApprovalError("SNAPSHOT_STALE")
        return self.reconcile(run_id, token, csrf)

    def get_receipt(self, run_id: str, token: str) -> ExecutionReceipt:
        with self._transaction(write=False) as db:
            session_hash, _ = self._session(db, token, as_utc(self._clock()))
            receipt = self._get_receipt(db, run_id, session_hash)
            if receipt is None:
                raise ApprovalError("RECEIPT_NOT_FOUND")
            return receipt

    def reconcile(self, run_id: str, token: str, csrf: str) -> ExecutionReceipt:
        """Verify a committed receipt without applying its actions again."""
        with self._transaction(write=True) as db:
            now = as_utc(self._clock())
            session_hash, _ = self._session(db, token, now, csrf=csrf)
            receipt = self._get_receipt(db, run_id, session_hash)
            if receipt is None:
                raise ApprovalError("RECEIPT_NOT_FOUND")
            if receipt.status in ("VERIFIED", "DIVERGED"):
                return receipt
            try:
                checked = verify_receipt(db, receipt, now=now)
            except (sqlite3.Error, ValueError, OverflowError):
                checked = receipt.model_copy(
                    update={"status": "UNVERIFIED", "uncertainties": ("RESULT_UNVERIFIED",)}
                )
            db.execute(
                "UPDATE receipts SET status = ?, receipt_json = ? WHERE run_id = ?",
                (checked.status, canonical_json(checked).decode(), run_id),
            )
            return checked

    @staticmethod
    def _get_receipt(
        db: sqlite3.Connection, run_id: str, session_hash: str
    ) -> ExecutionReceipt | None:
        row = db.execute(
            """SELECT receipts.status, receipts.receipt_json
               FROM receipts JOIN runs ON runs.id = receipts.run_id
               WHERE receipts.run_id = ? AND runs.session_hash = ?""",
            (run_id, session_hash),
        ).fetchone()
        if row is None:
            return None
        try:
            receipt = ExecutionReceipt.model_validate_json(row["receipt_json"])
        except ValueError as exc:
            raise ApprovalError("RECEIPT_INVALID") from exc
        if receipt.run_id != run_id or receipt.status != row["status"]:
            raise ApprovalError("RECEIPT_INVALID")
        return receipt

    @staticmethod
    def _fresh_snapshot(db: sqlite3.Connection, plan: ValidatedPlan) -> BoardSnapshot | None:
        row = db.execute("SELECT revision FROM boards WHERE id = ?", (plan.target_id,)).fetchone()
        if row is None or row["revision"] != plan.board_revision:
            return None
        try:
            current = capture_board(
                db, plan.target_id, row["revision"], clock=plan.simulation_clock
            )
        except ValueError:
            return None
        return current if current.fingerprint() == plan.snapshot_fingerprint else None

    def _exact_preview(self, snapshot: BoardSnapshot, plan: ValidatedPlan) -> PlanPreview:
        expected_contract = None
        if plan.proposer_type == "model":
            if self._model_planner is None:
                raise ApprovalError("MODEL_DISABLED")
            expected_contract = self._model_planner.contract_hash
        if plan.mcp_contract_hash != expected_contract:
            raise ApprovalError("TOOL_CONTRACT_CHANGED")
        try:
            preview = simulate_plan(snapshot, plan, self._policy)
        except PlanValidationError as exc:
            code = "POLICY_CHANGED" if exc.code == PlanErrorCode.POLICY_CHANGED else "PLAN_INVALID"
            raise ApprovalError(code) from exc
        if not all(isinstance(outcome, AllowedAction) for outcome in preview.simulation.outcomes):
            raise ApprovalError("PLAN_INVALID")
        return preview

    @staticmethod
    def _load(
        db: sqlite3.Connection, run_id: str, session_hash: str
    ) -> tuple[RunStatus, BoardSnapshot, ValidatedPlan]:
        row = db.execute(
            """SELECT COALESCE(receipts.status, runs.status) AS status,
                      runs.target_id, snapshots.canonical_json AS snapshot,
                      snapshots.fingerprint, plans.canonical_json AS plan, plans.digest
               FROM runs JOIN snapshots ON snapshots.run_id = runs.id
               JOIN plans ON plans.run_id = runs.id
               LEFT JOIN receipts ON receipts.run_id = runs.id
               WHERE runs.id = ? AND runs.session_hash = ?""",
            (run_id, session_hash),
        ).fetchone()
        if row is None:
            raise ApprovalError("RUN_NOT_FOUND")
        try:
            snapshot = BoardSnapshot.model_validate_json(row["snapshot"])
            plan = ValidatedPlan.model_validate_json(row["plan"])
        except ValueError as exc:
            raise ApprovalError("PLAN_INVALID") from exc
        if (
            snapshot.fingerprint() != row["fingerprint"]
            or plan.digest() != row["digest"]
            or plan.run_id != run_id
            or plan.target_id != row["target_id"]
            or plan.owner_id != OWNER_ID
        ):
            raise ApprovalError("PLAN_INVALID")
        return row["status"], snapshot, plan

    @staticmethod
    def _pending(status: RunStatus, plan: ValidatedPlan, digest: str, now: datetime) -> None:
        if status != "AWAITING_APPROVAL":
            raise ApprovalError("RUN_NOT_PENDING")
        if now < plan.created_at or now >= plan.expires_at:
            raise ApprovalError("APPROVAL_EXPIRED")
        if not compare_digest(digest, plan.digest()):
            raise ApprovalError("PLAN_DIGEST_MISMATCH")

    @staticmethod
    def _transition(
        db: sqlite3.Connection,
        run_id: str,
        status: RunStatus,
        now: datetime,
        *,
        expected: RunStatus = "AWAITING_APPROVAL",
    ) -> None:
        result = db.execute(
            """UPDATE runs SET status = ?, updated_at = ?
               WHERE id = ? AND status = ?""",
            (status, now.isoformat(), run_id, expected),
        )
        if result.rowcount != 1:
            raise ApprovalError("RUN_NOT_PENDING")

    @staticmethod
    def _review(status: RunStatus, preview: PlanPreview) -> RunReview:
        return RunReview(
            run_id=preview.plan.run_id,
            status=status,
            expires_at=preview.plan.expires_at,
            review=review_report(preview),
        )
