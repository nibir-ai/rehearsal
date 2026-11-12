"""Run the Rehearsal demo or browse a local IssueHub board."""

import argparse
import os
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from rehearsal.demo import preview_demo
from rehearsal.domain.actions import (
    Action,
    AddComment,
    AddLabel,
    CloseIssue,
    CreateIssue,
    referenced_issue_id,
)
from rehearsal.domain.models import BoardSnapshot
from rehearsal.domain.policy import Policy
from rehearsal.domain.simulator import BlockedAction, NoOpAction
from rehearsal.issuehub.reader import BoardNotFound, IssueHubReader
from rehearsal.issuehub.schema import upgrade_database
from rehearsal.issuehub.seed import seed_board


def _describe(action: Action) -> str:
    if isinstance(action, CloseIssue):
        return f"close {action.issue_id}"
    if isinstance(action, AddLabel):
        return f"add {action.label} to {action.issue_id}"
    if isinstance(action, AddComment):
        return f"comment on {action.issue_id}"
    return f"create {action.title}"


def _demo(snapshot: BoardSnapshot, *, json_output: bool, full: bool) -> None:
    preview = preview_demo(snapshot, full=full)
    plan = preview.plan
    clock = plan.simulation_clock

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
    for entry, outcome in zip(plan.actions, preview.simulation.outcomes, strict=True):
        action = entry.action
        kind = "close" if isinstance(action, CloseIssue) else action.kind
        target = referenced_issue_id(action) or "new issue"
        if isinstance(outcome, BlockedAction):
            detail = f"{outcome.reason_code}; no change"
        elif isinstance(outcome, NoOpAction):
            detail = f"{outcome.action.label} already present; no change"
        elif isinstance(action, CreateIssue):
            detail = f"create {outcome.after.id}; persistent ID assigned during execution"
        elif isinstance(action, AddComment):
            assert outcome.comment is not None
            detail = f"append {outcome.comment.id}; update issue activity"
        elif isinstance(action, CloseIssue) and outcome.before is not None:
            detail = f"{outcome.before.state} -> {outcome.after.state}"
        else:
            assert isinstance(action, AddLabel) and outcome.before is not None
            before = ", ".join(outcome.before.labels) or "(none)"
            after = ", ".join(outcome.after.labels)
            detail = f"add {action.label}; labels: {before} -> {after}"
        print(f"  {outcome.status} {kind} {target}  [{detail}]")
    print("SOURCE SNAPSHOT: unchanged (SHA-256 match)")
    print("LIVE WRITE COUNT: 0")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="rehearse the IssueHub fixture actions")
    source = demo.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture", type=Path, help="path to the board snapshot JSON")
    source.add_argument("--database", type=Path, help="path to a seeded IssueHub database")
    demo.add_argument("--at", help="explicit UTC or offset timestamp for a database capture")
    demo.add_argument("--json", action="store_true", help="print the exact plan and simulation")
    demo.add_argument("--full", action="store_true", help="include a follow-up and a recent issue")
    seed = commands.add_parser("seed", help="install a fixture into a local IssueHub database")
    seed.add_argument("--fixture", required=True, type=Path)
    seed.add_argument("--database", required=True, type=Path)
    migrate = commands.add_parser("migrate", help="upgrade an existing IssueHub database")
    migrate.add_argument("--database", required=True, type=Path)
    mcp = commands.add_parser("mcp", help="run the read-only IssueHub MCP server over stdio")
    mcp.add_argument("--database", required=True, type=Path)
    serve = commands.add_parser("serve", help="serve the read-only IssueHub API on loopback")
    serve.add_argument("--database", required=True, type=Path)
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--at", help="explicit UTC or offset timestamp for the review preview")
    serve.add_argument("--web-dist", type=Path, help="path to the built review page")
    serve.add_argument("--owner-approval", action="store_true", help="enable local owner approval")
    args = parser.parse_args(argv)

    try:
        if args.command == "migrate":
            upgrade_database(args.database)
            print("IssueHub schema is up to date.")
            return
        if args.command == "mcp":
            from rehearsal.issuehub.mcp_server import create_server

            create_server(args.database).run(transport="stdio")
            return
        if args.command == "serve":
            if not 1 <= args.port <= 65535:
                parser.error("--port must be between 1 and 65535")
            clock = datetime.fromisoformat(args.at) if args.at else None
            owner_secret = None
            if args.owner_approval:
                owner_secret = os.environ.get("REHEARSAL_OWNER_SECRET")
                if not owner_secret:
                    parser.error("--owner-approval requires REHEARSAL_OWNER_SECRET")
            _serve(
                args.database,
                args.port,
                clock=clock,
                web_dist=args.web_dist,
                owner_secret=owner_secret,
            )
            return
        if args.command == "seed":
            snapshot = BoardSnapshot.model_validate_json(args.fixture.read_bytes())
            if snapshot.board_id != Policy().allowed_board_id:
                parser.error("fixture board is outside the configured demo scope")
            seed_board(args.database, snapshot)
            print(f"Seeded {snapshot.board_id} at revision {snapshot.board_revision}.")
            return
        if args.fixture is not None:
            if args.at is not None:
                parser.error("--at requires --database")
            snapshot = BoardSnapshot.model_validate_json(args.fixture.read_bytes())
        else:
            board_id = Policy().allowed_board_id
            clock = datetime.fromisoformat(args.at) if args.at else datetime.now(UTC)
            snapshot = IssueHubReader(args.database, allowed_board_id=board_id).capture(
                board_id, clock=clock
            )
        _demo(snapshot, json_output=args.json, full=args.full)
    except (OSError, sqlite3.Error, BoardNotFound, ValueError, OverflowError) as exc:
        parser.error(str(exc))


def _serve(
    database: Path,
    port: int,
    *,
    clock: datetime | None,
    web_dist: Path | None,
    owner_secret: str | None,
) -> None:
    import uvicorn

    from rehearsal.issuehub.api import create_app

    app = create_app(database, clock=clock, web_dist=web_dist, owner_secret=owner_secret)
    uvicorn.run(app, host="127.0.0.1", port=port, proxy_headers=False)


if __name__ == "__main__":
    main()
