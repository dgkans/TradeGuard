import type { ReactNode } from "react";
import { errorText } from "../api";
import type { Decision } from "../types";

export function DecisionBadge({ decision }: { decision: Decision | string }) {
  const kind = decision.toLowerCase();
  return <span className={`badge badge-${kind}`}>{decision}</span>;
}

export function StatusBadge({ status }: { status: string }) {
  const tone =
    status === "executed" || status === "approved"
      ? "allow"
      : status === "pending" || status === "executing"
        ? "escalate"
        : "deny";
  return <span className={`badge badge-${tone}`}>{status}</span>;
}

export function ErrorNote({ error }: { error: unknown }) {
  if (error === null || error === undefined) return null;
  return (
    <p className="error" role="alert">
      {errorText(error)}
    </p>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function PageHeader({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="page-header">
      <h1>{title}</h1>
      {children ? <p className="lede">{children}</p> : null}
    </div>
  );
}
