import { useId, useState } from "react";
import { compactJson, orderSummary, otherArgs, time } from "../format";
import type { JsonObject } from "../types";
import { ErrorNote } from "./common";

export interface ApprovalView {
  id: string;
  tool: string;
  args?: JsonObject;
  rule_id: string;
  reason: string;
  expires_at?: string;
  session_id?: string;
}

interface Props {
  approval: ApprovalView;
  /** Accessible name of the card, e.g. "place_order NVDA". */
  title: string;
  onDecide: (approve: boolean, note: string) => Promise<void>;
  error?: unknown;
  /** Trader view: show the title as a heading and plain words only (no rule ids or args). */
  trader?: boolean;
}

/** One escalated call: what the agent wants to do, why the gateway held it, approve/reject. */
export function ApprovalCard({ approval, title, onDecide, error, trader = false }: Props) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const noteId = useId();
  const headingId = useId();
  const extra = otherArgs(approval.args);

  async function decide(approve: boolean) {
    setBusy(true);
    try {
      await onDecide(approve, note);
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="card approval-card" aria-labelledby={headingId}>
      <h3 id={headingId} className={trader ? "card-title" : "visually-hidden"}>
        {title}
      </h3>
      <div className="approval-order">
        <span className="order-text">{orderSummary(approval.tool, approval.args)}</span>
        {trader ? null : <code className="muted">{approval.tool}</code>}
      </div>
      {!trader && Object.keys(extra).length > 0 ? (
        <div className="args">{compactJson(extra)}</div>
      ) : null}
      <dl className="facts">
        {trader ? null : (
          <>
            <dt>Rule</dt>
            <dd>
              <code>{approval.rule_id}</code>
            </dd>
          </>
        )}
        <dt>Why it was held</dt>
        <dd>{approval.reason}</dd>
        {approval.expires_at ? (
          <>
            <dt>Expires</dt>
            <dd>{time(approval.expires_at)}</dd>
          </>
        ) : null}
        {approval.session_id ? (
          <>
            <dt>Session</dt>
            <dd>{approval.session_id}</dd>
          </>
        ) : null}
      </dl>
      <label htmlFor={noteId} className="field-label">
        Note (optional)
      </label>
      <input
        id={noteId}
        className="input"
        value={note}
        maxLength={2000}
        onChange={(event) => setNote(event.target.value)}
        placeholder="Why you decided"
      />
      <div className="actions">
        <button type="button" className="button button-approve" disabled={busy}
                onClick={() => void decide(true)}>
          Approve
        </button>
        <button type="button" className="button button-reject" disabled={busy}
                onClick={() => void decide(false)}>
          Reject
        </button>
      </div>
      <ErrorNote error={error} />
    </article>
  );
}
