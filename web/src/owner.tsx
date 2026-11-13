import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import type { Report, Value } from "./main";

type ReceiptStatus = "APPLIED_UNVERIFIED" | "VERIFIED" | "DIVERGED" | "UNVERIFIED";
type RunStatus = "AWAITING_APPROVAL" | "APPROVED" | "REJECTED" | "STALE" | ReceiptStatus;
type Run = { run_id: string; status: RunStatus; expires_at: string; review: Report };
type Receipt = {
  status: ReceiptStatus;
  attempted_at: string;
  expected_board_revision: number;
  observed_board_revision: number | null;
  uncertainties: string[];
  operations: {
    action_id: string;
    kind: string;
    target: string;
    temporary_id: string | null;
    comment_id: number | null;
    predicted: Record<string, Value>;
    observed: Record<string, Value> | null;
    verification: "PENDING" | "MATCH" | "MISMATCH";
  }[];
};
type Session = { csrf: string; runId?: string };

const sessionKey = "rehearsal.owner-session";
const statusText: Record<RunStatus, string> = {
  AWAITING_APPROVAL: "Awaiting approval",
  APPROVED: "Approved",
  REJECTED: "Rejected",
  STALE: "Board changed",
  APPLIED_UNVERIFIED: "Applied · Verification pending",
  VERIFIED: "Verified",
  DIVERGED: "Observed changes differ",
  UNVERIFIED: "Readback unavailable",
};
const errors: Record<string, string> = {
  OWNER_AUTH_REQUIRED: "Check your owner secret or sign in again.",
  CSRF_INVALID: "This tab no longer has the active owner session. Sign in again.",
  APPROVAL_EXPIRED: "The plan expired. Create a fresh review.",
  SNAPSHOT_STALE: "The board changed. Create a fresh review.",
  POLICY_CHANGED: "The policy changed. Create a fresh review.",
  PLAN_DIGEST_MISMATCH: "The digest did not match the saved plan. Reload the run.",
  RUN_NOT_PENDING: "The run already has a decision. Reload the run.",
  RUN_NOT_APPROVED: "This run needs approval before execution.",
  RUN_NOT_FOUND: "This run is unavailable to the current owner session.",
  MIGRATION_REQUIRED: "Run the IssueHub migrate command, then retry.",
  NO_APPROVABLE_ACTIONS: "The board has no actions eligible for approval.",
  BOARD_NOT_FOUND: "Seed the IssueHub demo board, then retry.",
  MODEL_UNAVAILABLE: "The local model did not respond. Check Ollama or create a fixture review.",
  MODEL_OUTPUT_INVALID: "The model returned an invalid plan. Try a different goal.",
  MODEL_PROPOSAL_REJECTED: "The proposal failed validation. Try a different goal.",
  MODEL_CONTEXT_TOO_LARGE: "The captured board is too large for model planning.",
  MODEL_CONTEXT_UNAVAILABLE: "The model could not read the captured board. Try again.",
  TOOL_CONTRACT_CHANGED: "The IssueHub read tools changed. Restart the server and create a fresh review.",
  MODEL_DISABLED: "Start Rehearsal with a local model to use model planning.",
};

class RequestError extends Error {
  constructor(readonly code: string) {
    super(errors[code] ?? "The request could not be completed. Reload the run to check its status.");
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...init, credentials: "same-origin", cache: "no-store" });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new RequestError(typeof body?.detail === "string" ? body.detail : "");
  }
  return await response.json() as T;
}

function post<T>(path: string, csrf: string, body?: object) {
  const headers: Record<string, string> = { "X-Rehearsal-CSRF": csrf };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  return request<T>(path, {
    method: "POST", headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function readSession(): Session | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(sessionKey) ?? "null");
    return typeof value?.csrf === "string" ? value : null;
  } catch {
    return null;
  }
}

function hasReceipt(status: RunStatus): status is ReceiptStatus {
  return status === "VERIFIED" || status === "DIVERGED" ||
    status === "UNVERIFIED" || status === "APPLIED_UNVERIFIED";
}

function receiptValue(value: Value | undefined) {
  if (value == null || value === "") return "—";
  if (Array.isArray(value)) return value.join(", ") || "None";
  return String(value);
}

function ReceiptView({ receipt }: { receipt: Receipt }) {
  return (
    <section className="owner-panel receipt" aria-labelledby="receipt-heading">
      <h2 id="receipt-heading">Execution receipt</h2>
      <p role="status">{statusText[receipt.status]}</p>
      <p>Attempted <code>{receipt.attempted_at}</code></p>
      <p>Board revision <code>{receiptValue(receipt.observed_board_revision)}</code>
        {" / expected "}<code>{receipt.expected_board_revision}</code></p>
      {receipt.uncertainties.length > 0 && <p><code>{receipt.uncertainties.join(", ")}</code></p>}
      <ol className="receipt-operations">
        {receipt.operations.map((op) => (
          <li key={op.action_id}>
            <details>
              <summary>
                <code>{op.target}</code> {op.kind.replaceAll("_", " ")} · {
                  { MATCH: "Matches prediction", MISMATCH: "Differs from prediction", PENDING: "Awaiting readback" }[op.verification]
                }
              </summary>
              {op.temporary_id && <p>Temporary ID <code>{op.temporary_id}</code></p>}
              {op.comment_id !== null && <p>Comment ID <code>{op.comment_id}</code></p>}
              <table>
                <thead><tr><th scope="col">Field</th><th scope="col">Predicted</th><th scope="col">Observed</th></tr></thead>
                <tbody>
                  {Object.entries(op.predicted).map(([field, expected]) => (
                    <tr key={field}>
                      <th scope="row">{field.replaceAll("_", " ")}</th>
                      <td><code>{receiptValue(expected)}</code></td>
                      <td><code>{receiptValue(op.observed?.[field])}</code></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function OwnerWorkflow({ renderReview, localModel, downloadJson }: {
  renderReview: (report: Report) => ReactNode;
  localModel: string | null;
  downloadJson: (value: object, filename: string) => void;
}) {
  const [session, setSession] = useState(readSession);
  const [run, setRun] = useState<Run | null>(null);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [error, setError] = useState<RequestError | null>(null);
  const [busy, setBusy] = useState(false);
  const [useModel, setUseModel] = useState(false);
  const [goal, setGoal] = useState("");
  const locked = useRef(false);
  const runPath = (id: string) => "/api/runs/" + encodeURIComponent(id);

  function save(next: Session | null) {
    if (next) sessionStorage.setItem(sessionKey, JSON.stringify(next));
    else sessionStorage.removeItem(sessionKey);
    setSession(next);
  }

  async function perform(task: () => Promise<void>) {
    if (locked.current) return;
    locked.current = true;
    setBusy(true);
    setError(null);
    try {
      await task();
    } catch (failure) {
      setError(failure instanceof RequestError ? failure : new RequestError(""));
    } finally {
      locked.current = false;
      setBusy(false);
    }
  }

  async function loadRun(id: string, signal?: AbortSignal) {
    const path = runPath(id);
    const saved = await request<Run>(path + "/preview", { signal });
    const result = hasReceipt(saved.status) ? await request<Receipt>(path + "/receipt", { signal }) : null;
    if (!signal?.aborted) {
      setRun(result ? { ...saved, status: result.status } : saved);
      setReceipt(result);
    }
  }

  const initialRun = useRef(session?.runId);
  useEffect(() => {
    const id = initialRun.current;
    if (!id) return;
    const controller = new AbortController();
    void perform(async () => {
      try {
        await loadRun(id, controller.signal);
      } catch (failure) {
        if (!controller.signal.aborted) throw failure;
      }
    });
    return () => controller.abort();
  }, []);

  function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const secret = String(new FormData(event.currentTarget).get("secret") ?? "");
    event.currentTarget.reset();
    void perform(async () => {
      const result = await request<{ csrf_token: string }>("/api/owner/session", {
        method: "POST",
        headers: { Authorization: "Bearer " + secret },
      });
      save({ csrf: result.csrf_token });
      setRun(null);
      setReceipt(null);
    });
  }

  function create() {
    if (!session) return;
    const model = Boolean(localModel && useModel);
    if (model && !goal.trim()) return;
    void perform(async () => {
      const result = await post<Run>("/api/runs", session.csrf, {
        target_id: "demo/platform",
        goal: model ? goal.trim() : "Rehearse stale-issue maintenance",
        use_model: model,
      });
      save({ ...session, runId: result.run_id });
      setRun(result);
      setReceipt(null);
    });
  }

  function decide(decision: "approve" | "reject") {
    if (!run || !session) return;
    void perform(async () => {
      const result = await post<{ status: "APPROVED" | "REJECTED" }>(
        runPath(run.run_id) + "/" + decision, session.csrf,
        { decision, plan_digest: run.review.plan_digest },
      );
      setRun({ ...run, status: result.status });
    });
  }

  function execution(step: "execute" | "reconcile") {
    if (!run || !session) return;
    void perform(async () => {
      const result = await post<Receipt>(runPath(run.run_id) + "/" + step, session.csrf);
      setReceipt(result);
      setRun({ ...run, status: result.status });
    });
  }

  const loginNeeded = !session || error?.code === "OWNER_AUTH_REQUIRED" || error?.code === "CSRF_INVALID";
  const restartNeeded = error !== null && [
    "APPROVAL_EXPIRED", "SNAPSHOT_STALE", "POLICY_CHANGED", "RUN_NOT_FOUND",
  ].includes(error.code);
  const hasDecision = run && !restartNeeded && (
    run.status === "AWAITING_APPROVAL" || run.status === "APPROVED" ||
    run.status === "APPLIED_UNVERIFIED" || run.status === "UNVERIFIED"
  );
  const canCreate = !run || restartNeeded || run.status === "REJECTED" ||
    run.status === "STALE" || run.status === "VERIFIED" || run.status === "DIVERGED";

  return (
    <div className="owner-workflow">
      <h1>Owner workflow</h1>
      {busy && <p role="status">Working…</p>}
      {error && <p className="warn" role="alert">{error.message}</p>}
      {loginNeeded ? (
        <form className="owner-panel owner-signin" method="post" onSubmit={signIn}>
          <label htmlFor="owner-secret">Local owner secret</label>
          <input id="owner-secret" name="secret" type="password" autoComplete="off" required />
          <button className="export" disabled={busy}>Sign in</button>
        </form>
      ) : (
        <>
          {canCreate && localModel && (
            <section className="owner-panel model-options" aria-label="Planning options">
              <label>
                <input type="checkbox" checked={useModel} disabled={busy}
                  onChange={(event) => setUseModel(event.target.checked)} />
                Use local model <code>{localModel}</code>
              </label>
              {useModel && <>
                <label htmlFor="model-goal">Planning goal</label>
                <textarea id="model-goal" maxLength={500} rows={3} value={goal}
                  onChange={(event) => setGoal(event.target.value)} disabled={busy} />
                <p>Rehearsal sends your goal, issue titles, labels, and status metadata to Ollama on this machine.</p>
              </>}
            </section>
          )}
          <div className="owner-controls">
            {canCreate && (
              <button disabled={busy || Boolean(localModel && useModel && !goal.trim())}
                onClick={create}>Create review</button>
            )}
            {session.runId && <button disabled={busy} onClick={() => {
              const id = session.runId;
              if (id) void perform(() => loadRun(id));
            }}>Reload run</button>}
            {run && <span role="status">{statusText[run.status]}</span>}
          </div>
          {run && renderReview(run.review)}
          {run && hasDecision && (
            <section className="owner-panel" aria-label="Owner decision">
              {run.status === "AWAITING_APPROVAL" && (
                <>
                  <h2>Approve this exact plan</h2>
                  <p>Review expires <code>{run.expires_at}</code></p>
                  <div className="owner-controls">
                    <button className="export" disabled={busy} onClick={() => decide("approve")}>Approve plan</button>
                    <button disabled={busy} onClick={() => decide("reject")}>Reject plan</button>
                  </div>
                </>
              )}
              {run.status === "APPROVED" && (
                <>
                  <h2>Apply approved changes</h2>
                  <p>Execution writes the reviewed changes to the local IssueHub board.</p>
                  <div className="owner-controls">
                    <button className="export" disabled={busy} onClick={() => execution("execute")}>Execute approved plan</button>
                  </div>
                </>
              )}
              {(run.status === "APPLIED_UNVERIFIED" || run.status === "UNVERIFIED") && (
                <button disabled={busy} onClick={() => execution("reconcile")}>Retry readback</button>
              )}
            </section>
          )}
          {receipt && run && (
            <>
              <ReceiptView receipt={receipt} />
              <div className="owner-controls">
                <button onClick={() => downloadJson(receipt, `rehearsal-receipt-${run.run_id}.json`)}>
                  Download receipt
                </button>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
