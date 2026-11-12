import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./review.css";

type Value = string | number | boolean | string[] | null;
type Change = { field: string; before: Value; after: Value };
type Evidence = { issue_id?: string; field: string; expected_value: Value };
type Status = "ALLOWED" | "BLOCKED" | "NO_OP";
type Action =
  | { kind: "close_issue"; issue_id: string; reason: string }
  | { kind: "add_label"; issue_id: string; label: string }
  | { kind: "create_issue"; title: string; body: string; source_issue_id: string | null }
  | { kind: "add_comment"; issue_id: string; body: string };

type ReviewAction = {
  id: string;
  action: Action;
  status: Status;
  issue_id: string | null;
  issue_title: string | null;
  current_state: "open" | "closed" | null;
  temporary_id: string | null;
  reason_code: string | null;
  changes: Change[];
  evidence: Evidence[];
};
type Report = {
  board_id: string;
  board_revision: number;
  simulation_clock: string;
  plan_digest: string;
  actions: ReviewAction[];
};
type LoadState =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; report: Report };
type Filter = "all" | "allowed" | "blocked";

const labels: Record<Action["kind"], string> = {
  close_issue: "Close issue",
  add_label: "Add label",
  create_issue: "Create issue",
  add_comment: "Add comment",
};
const statuses: Record<Status, string> = {
  ALLOWED: "Allowed",
  BLOCKED: "Blocked",
  NO_OP: "No change",
};
const reasons: Record<string, string> = {
  PROTECTED_LABEL: "A protected label prevents closure.",
  RECENT_ACTIVITY: "This issue has been active within the last 30 days.",
  ALREADY_CLOSED: "The issue is already closed.",
  MISSING_RESOLUTION_EVIDENCE: "The snapshot has no maintainer resolution evidence.",
  UNKNOWN_LABEL: "The label is absent from the board registry.",
  ISSUE_NOT_FOUND: "The issue is absent from the captured board.",
};
const fields: Record<string, string> = {
  id: "Issue ID",
  board_id: "Board",
  title: "Title",
  body: "Description",
  state: "State",
  labels: "Labels",
  label_registry: "Label registry",
  revision: "Revision",
  last_activity_at: "Last activity",
  resolved_by_maintainer: "Maintainer confirmed resolution",
};
const filters: { id: Filter; label: string }[] = [
  { id: "all", label: "All actions" },
  { id: "allowed", label: "Allowed" },
  { id: "blocked", label: "Blocked" },
];

function display(value: Value): string {
  if (value === null || value === "") return "—";
  if (Array.isArray(value)) return value.join(", ") || "None";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

function explanation(action: ReviewAction): string {
  if (action.status === "NO_OP") return "The label is already present.";
  if (action.reason_code) return reasons[action.reason_code] ?? "The policy blocked this action.";
  if (action.temporary_id) return "IssueHub assigns a permanent ID during execution.";
  return "The action passed the simulation policy.";
}

function compare(row: ReviewAction): [Value, Value] {
  const field = {
    close_issue: "state",
    add_label: "labels",
    create_issue: "id",
    add_comment: "last_activity_at",
  }[row.action.kind];
  if (row.status === "BLOCKED") {
    const baseline = field === "state" ? row.current_state :
      row.evidence.find((entry) => entry.field === field)?.expected_value ?? null;
    return [baseline, "Unchanged"];
  }
  if (row.status === "NO_OP" && row.action.kind === "add_label") {
    return [row.action.label, row.action.label];
  }
  const change = row.changes.find((entry) => entry.field === field);
  return change ? [change.before, change.after] : [null, null];
}

function download(report: Report) {
  const file = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(file);
  const link = document.createElement("a");
  link.href = url;
  link.download = `rehearsal-r${report.board_revision}-${report.plan_digest.slice(0, 8)}.json`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function Inspector({ action }: { action: ReviewAction | undefined }) {
  return (
    <aside className="inspector" id="action-detail" aria-labelledby="detail-title" tabIndex={-1}>
      <div className="inspector-heading">
        <h2 id="detail-title">Action details</h2>
        {action && <button className="back-to-actions" onClick={() => {
          document.querySelector<HTMLButtonElement>(".row.selected .row-select")?.focus();
        }}>Back to action</button>}
      </div>
      {action ? (
        <>
          <div className="decision">
            <div className="decision-label">
              <h3>{labels[action.action.kind]} {action.issue_id && <code>{action.issue_id}</code>}</h3>
              <span className={`status ${action.status.toLowerCase()}`}>{statuses[action.status]}</span>
            </div>
            <p>{explanation(action)}</p>
            {action.reason_code && <code>{action.reason_code}</code>}
            {action.temporary_id && (
              <p className="temporary">Temporary ID <code>{action.temporary_id}</code></p>
            )}
            {action.action.kind === "create_issue" && action.action.source_issue_id && (
              <p className="temporary">Source issue <code>{action.action.source_issue_id}</code></p>
            )}
          </div>
          <section className="evidence" aria-labelledby="evidence-title">
            <h3 id="evidence-title">Captured evidence</h3>
            <dl>
              {action.evidence.map((entry) => (
                <div key={entry.field}>
                  <dt>{fields[entry.field] ?? entry.field}</dt>
                  <dd>{display(entry.expected_value)}</dd>
                </div>
              ))}
            </dl>
          </section>
          {action.changes.length > 0 && (
            <details className="changes">
              <summary>All field changes <span>{action.changes.length}</span></summary>
              <dl>
                {action.changes.map((change) => (
                  <div key={change.field}>
                    <dt>{fields[change.field] ?? change.field}</dt>
                    <dd><span className="change-label">Before</span> {display(change.before)}</dd>
                    <dd><span className="change-label">After</span> {display(change.after)}</dd>
                  </div>
                ))}
              </dl>
            </details>
          )}
        </>
      ) : <p className="empty">Choose an action to inspect its evidence.</p>}
    </aside>
  );
}

function Review({ report }: { report: Report }) {
  const [query, setQuery] = useState(() => new URLSearchParams(window.location.search));
  useEffect(() => {
    const sync = () => setQuery(new URLSearchParams(window.location.search));
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  const requested = query.get("view");
  const filter: Filter = requested === "allowed" || requested === "blocked" ? requested : "all";
  const visible = report.actions.filter((action) => filter === "all" || action.status.toLowerCase() === filter);
  const selected = visible.find((row) => row.id === query.get("action")) ?? visible[0];
  const allowed = report.actions.filter((row) => row.status === "ALLOWED").length;
  const blocked = report.actions.filter((row) => row.status === "BLOCKED").length;

  function update(view: Filter, action?: string) {
    const next = new URLSearchParams();
    if (view !== "all") next.set("view", view);
    if (action) next.set("action", action);
    const search = next.toString();
    if (search === new URLSearchParams(window.location.search).toString()) return;
    window.history.pushState(null, "", search ? `?${search}` : window.location.pathname);
    setQuery(next);
  }

  function selectAction(id: string) {
    update(filter, id);
    if (window.matchMedia("(max-width: 850px)").matches) {
      document.getElementById("action-detail")?.focus();
    }
  }

  return (
    <>
      <header className="page-heading">
        <div>
          <h1>Review simulation</h1>
          <p>Inspect proposed IssueHub changes and their policy decisions.</p>
        </div>
        <button className="export" onClick={() => download(report)}>Download report</button>
      </header>
      <dl className="summary">
        <div><dt>Board</dt><dd><code>{report.board_id}</code></dd></div>
        <div><dt>Revision</dt><dd><code>{report.board_revision}</code></dd></div>
        <div><dt>Predicted changes</dt><dd className="good"><code>{allowed}</code></dd></div>
        <div><dt>Blocked actions</dt><dd className="warn"><code>{blocked}</code></dd></div>
      </dl>
      <div className="workspace">
        <section className="ledger" aria-labelledby="ledger-title">
          <div className="ledger-heading">
            <h2 id="ledger-title">Change set</h2>
            <div className="filters" role="group" aria-label="Filter by outcome">
              {filters.map(({ id, label }) => (
                <button key={id} aria-pressed={filter === id} onClick={() => update(id)}>{label}</button>
              ))}
            </div>
          </div>
          {visible.length > 0 ? (
            <>
              <div className="ledger-columns" aria-hidden="true"><span>Action</span><span>Before</span><span>Predicted after</span></div>
              <ol className="rows" role="list">
                {visible.map((row) => {
                  const [before, after] = compare(row);
                  return (
                    <li key={row.id} className={`row ${row.id === selected?.id ? "selected" : ""}`}>
                      <div className="row-action">
                        <span className="sequence" aria-label={`Plan action ${report.actions.indexOf(row) + 1}`}>
                          {report.actions.indexOf(row) + 1}
                        </span>
                        <button className="row-select" aria-pressed={row.id === selected?.id} aria-controls="action-detail"
                          onClick={() => selectAction(row.id)}>
                          <span>{labels[row.action.kind]}</span>
                          <code>{row.issue_id ?? row.temporary_id ?? "New issue"}</code>
                        </button>
                        <span className="issue-title">{row.issue_title}</span>
                      </div>
                      <div className="value"><span className="mobile-label">Before</span><code>{display(before)}</code></div>
                      <div className="value">
                        <span className="mobile-label">Predicted after</span>
                        <code className={row.status === "ALLOWED" ? "good" : ""}>{display(after)}</code>
                        <span className={`status ${row.status.toLowerCase()}`}>{statuses[row.status]}</span>
                      </div>
                    </li>
                  );
                })}
              </ol>
            </>
          ) : (
            <div className="empty">
              <p>No actions match this filter.</p>
              <button onClick={() => update("all")}>Show all actions</button>
            </div>
          )}
        </section>
        <Inspector action={selected} />
      </div>
      <footer className="report-footer">
        <span>No live writes</span>
        <details><summary>Snapshot details</summary>
          <dl>
            <div><dt>Simulation clock</dt><dd><code>{report.simulation_clock}</code></dd></div>
            <div><dt>Plan digest</dt><dd><code>{report.plan_digest}</code></dd></div>
          </dl>
        </details>
      </footer>
    </>
  );
}

function App() {
  const [load, setLoad] = useState<LoadState>({ state: "loading" });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const request = new AbortController();
    fetch("/api/rehearsal/preview", { signal: request.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(
          response.status === 404 ? "Seed the IssueHub demo board, then try again." :
          response.status === 422 ? "The current board lacks issues required by the demo plan." :
          "Check that IssueHub is running with a seeded database.",
        );
        return await response.json() as Report;
      })
      .then((report) => setLoad({ state: "ready", report }))
      .catch((error: unknown) => {
        if (!request.signal.aborted) {
          setLoad({ state: "error", message: error instanceof Error ? error.message : "Try loading again." });
        }
      });
    return () => request.abort();
  }, [attempt]);

  return (
    <>
      <header className="site-header"><div className="container chrome">
        <div className="brand"><span aria-hidden="true" className="mark">R</span> Rehearsal</div>
        <span className="local">Local review</span>
      </div></header>
      <main className="container content">
        {load.state === "ready" ? <Review report={load.report} /> : (
          <>
            <h1>Review simulation</h1>
            {load.state === "loading" ? (
              <div className="loading" role="status">
                <p>Loading simulation…</p>
                <div aria-hidden="true" className="skeleton"><i /><i /><i /></div>
              </div>
            ) : (
              <div className="load-error" role="alert">
                <h2>Preview unavailable</h2>
                <p>{load.message}</p>
                <button onClick={() => {
                  setLoad({ state: "loading" });
                  setAttempt((count) => count + 1);
                }}>Try again</button>
              </div>
            )}
          </>
        )}
      </main>
    </>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
