# Rehearsal

Rehearsal previews what proposed IssueHub actions would change. The simulator supports issue
closure, label addition, comments, and new issues against a fixed snapshot.

Run these commands from the repository root with Python 3.12 and uv. See the
[uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).

```sh
uv sync --locked --no-dev --python 3.12
uv run --locked --no-dev python -m rehearsal.cli demo --fixture fixtures/boards/platform.json
```

The initial dependency setup may need network access. The fixture demo runs offline after
installation and requires no account, API key, database, or running service.

The [five-issue fixture](fixtures/boards/platform.json) represents `demo/platform` at revision
17, captured at `2026-11-12T12:00:00Z`. The demo predicts closing ISS-104 and adding
`needs-follow-up` to ISS-108. It blocks closing ISS-112 because of its `security` label.
The report shows before/after values and checks that the original snapshot's fingerprint
stays the same. Pass `--json` to the demo to inspect the exact plan, captured evidence,
digest, and structured simulation.

Pass `--full` to also preview a follow-up for ISS-108 and a rejected closure for recently
active ISS-117. Predicted issues and comments have temporary `tmp:` IDs. Adding a comment
updates predicted issue activity, so a later closure of the same issue can be blocked.
The default plan limit is five actions, including up to two new issues. The preview rejects
common credential patterns in proposed text. This check covers a limited set of patterns.

The simulator evaluates a fixed batch in order against a copy of the source state. A closure
needs an open issue, a trusted resolution flag, and at least 30 full days of inactivity.
`security`, `legal-hold`, and `needs-human` protect issues from closure. Labels must exist
in the board registry. Adding an existing label is a no-op. A successful label addition
updates the predicted issue revision and preserves its activity timestamp. The predicted
state retains the source board revision because no changes were committed.

The fixture proposal is deterministic. Before preview, Rehearsal checks its evidence against
the captured issues and binds the plan to the snapshot, policy, action schema, and simulation
clock. The digest identifies those exact inputs and the ordered actions. The rehearsal
performs zero live writes.

## Local IssueHub

Seed a SQLite board and rehearse against a consistent read of its current state.

```sh
uv run --locked --no-dev python -m rehearsal.cli seed --database issuehub.sqlite --fixture fixtures/boards/platform.json
uv run --locked --no-dev python -m rehearsal.cli demo --database issuehub.sqlite --at 2026-11-12T12:00:00Z
```

Seeding refuses to overwrite an existing board. The capture uses the current clock by
default. An explicit `--at` sets the clock for the current state and supports repeatable
previews. It does not reconstruct older issue versions.

Start the read-only listing API with `uv run --locked --no-dev python -m rehearsal.cli serve --database issuehub.sqlite`.
Open [the issue list](http://127.0.0.1:8000/api/issuehub/issues?limit=2) to see the board
revision and a page of issue summaries. Pass the returned `next_cursor` as the `cursor`
query parameter to continue. The API also accepts `state=open` or `state=closed` and
allows up to 50 issues per page. A revision change returns HTTP 409 for an old cursor.
Restart the listing without a cursor to read the new revision.

## IssueHub MCP server

Configure a local MCP client to launch this command over stdio. Use an absolute database
path if the client starts the process from a different directory.

```sh
uv run --locked --no-dev python -m rehearsal.cli mcp --database issuehub.sqlite
```

The server accepts `board_id: "demo/platform"` and provides five read tools: `list_issues`,
`get_issue`, `get_issue_comments`, `list_labels`, and `get_board_revision`.
Comments are paginated and capped at 1,000 characters per body. The demo fixture starts
with an empty comment history.

For a database seeded before comment support, run
`uv run --locked --no-dev python -m rehearsal.cli migrate --database issuehub.sqlite`
to enable comment reads while preserving the existing board.

## Browser review

After seeding the SQLite demo board, build the review page with Node.js 22.12 or later
and serve it from the same loopback process as IssueHub.

```sh
npm --prefix web ci
npm --prefix web run build
uv run --locked --no-dev python -m rehearsal.cli serve --database issuehub.sqlite --at 2026-11-12T12:00:00Z --web-dist web/dist
```

Open [Rehearsal](http://127.0.0.1:8000/) to compare changes, inspect captured evidence,
filter policy outcomes, and download the report you reviewed. The page previews three
changes and explains two blocked actions for the seeded board.
