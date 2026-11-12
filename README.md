# Rehearsal

Rehearsal previews what proposed IssueHub actions would change. The local demo supports issue
closure and label addition against a fixed snapshot.

Run these commands from the repository root with Python 3.12 and uv. See the
[uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).

```sh
uv sync --locked --no-dev --python 3.12
uv run --locked --no-dev python -m rehearsal.cli demo --fixture fixtures/boards/platform.json
```

The initial dependency setup may need network access. After setup, the demo runs offline and
requires no account, API key, database, or running service.

The [five-issue fixture](fixtures/boards/platform.json) represents `demo/platform` at revision
17, captured at `2026-11-12T12:00:00Z`. The demo predicts closing ISS-104 and adding
`needs-follow-up` to ISS-108. It blocks closing ISS-112 because of its `security` label.
The report shows before/after values and checks that the original snapshot's fingerprint
stays the same. Pass `--json` to the demo to inspect the exact plan, captured evidence,
digest, and structured simulation.

The simulator evaluates a fixed batch in order against a copy of the source state. A closure
needs an open issue, a trusted resolution flag, and at least 30 full days of inactivity.
`security`, `legal-hold`, and `needs-human` protect issues from closure. Labels must exist
in the board registry. Adding an existing label is a no-op. A successful label addition
updates the predicted issue revision and preserves its activity timestamp. The predicted
state retains the source board revision because no changes were committed.

The fixture proposal is deterministic. Before preview, Rehearsal checks its evidence against
the captured issues and binds the plan to the snapshot, policy, action schema, and simulation
clock. The digest identifies those exact inputs and the ordered actions. The demo ends at
preview and performs zero live writes.
