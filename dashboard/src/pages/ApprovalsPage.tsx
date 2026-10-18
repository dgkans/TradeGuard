import { useCallback, useEffect, useId, useState } from "react";
import { useApi } from "../api";
import { ApprovalCard } from "../components/ApprovalCard";
import { Empty, ErrorNote, PageHeader, StatusBadge } from "../components/common";
import { orderSummary, time } from "../format";
import { reviewerName, useReviewer } from "../reviewer";
import type { Approval } from "../types";

const RECENT = 10;

function byNewest(a: Approval, b: Approval): number {
  return (b.decided_at ?? b.created_at).localeCompare(a.decided_at ?? a.created_at);
}

/** Pending queue with approve/reject, plus the most recent decisions. */
export function ApprovalsPage({ pollMs = 3000 }: { pollMs?: number }) {
  const api = useApi();
  const [reviewer, setReviewer] = useReviewer();
  const [items, setItems] = useState<Approval[] | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [cardErrors, setCardErrors] = useState<Record<string, unknown>>({});
  const nameId = useId();
  const recentId = useId();

  const refresh = useCallback(async () => {
    try {
      setItems(await api.listApprovals());
      setLoadError(null);
    } catch (error) {
      setLoadError(error);
    }
  }, [api]);

  useEffect(() => {
    void refresh();
    if (pollMs <= 0) return undefined;
    const timer = window.setInterval(() => void refresh(), pollMs);
    return () => window.clearInterval(timer);
  }, [refresh, pollMs]);

  async function decide(item: Approval, approve: boolean, note: string) {
    const name = reviewerName(reviewer);
    try {
      if (approve) await api.approve(item.id, name, note);
      else await api.reject(item.id, name, note);
      setCardErrors((current) =>
        Object.fromEntries(Object.entries(current).filter(([id]) => id !== item.id)),
      );
    } catch (error) {
      setCardErrors((current) => ({ ...current, [item.id]: error }));
    }
    await refresh();
  }

  const pending = (items ?? []).filter((item) => item.status === "pending");
  const recent = (items ?? []).filter((item) => item.status !== "pending").sort(byNewest);

  return (
    <section>
      <PageHeader title="Approvals">
        Calls the gateway held for a human. Approving runs the stored order exactly once, after
        the deny rules are checked again.
      </PageHeader>
      <div className="toolbar">
        <label htmlFor={nameId} className="field-label">
          Your name
        </label>
        <input id={nameId} className="input input-short" value={reviewer} maxLength={100}
               onChange={(event) => setReviewer(event.target.value)} />
      </div>
      <ErrorNote error={loadError} />
      {items !== null && pending.length === 0 ? (
        <Empty>Nothing is waiting for approval.</Empty>
      ) : null}
      <div className="card-grid">
        {pending.map((item) => (
          <ApprovalCard
            key={item.id}
            approval={item}
            title={`${item.tool} ${typeof item.args.symbol === "string" ? item.args.symbol : ""}`}
            error={cardErrors[item.id]}
            onDecide={(approve, note) => decide(item, approve, note)}
          />
        ))}
      </div>
      {recent.length > 0 ? (
        <section aria-labelledby={recentId} className="recent">
          <h2 id={recentId}>Recent decisions</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Decided</th>
                  <th scope="col">Order</th>
                  <th scope="col">Rule</th>
                  <th scope="col">Status</th>
                  <th scope="col">By and note</th>
                </tr>
              </thead>
              <tbody>
                {recent.slice(0, RECENT).map((item) => (
                  <tr key={item.id}>
                    <td className="nowrap muted">{time(item.decided_at)}</td>
                    <td>{orderSummary(item.tool, item.args)}</td>
                    <td>
                      <code>{item.rule_id}</code>
                    </td>
                    <td>
                      <StatusBadge status={item.status} />
                    </td>
                    <td>
                      {item.decided_by ?? "–"}
                      {item.note ? <span className="muted"> · {item.note}</span> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}
    </section>
  );
}
