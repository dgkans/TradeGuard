import { vi } from "vitest";
import type { Api } from "../api";
import type { Approval, AuditEvent } from "../types";

/** An Api whose every method fails until a test gives it an implementation. */
export function fakeApi(overrides: Partial<Api> = {}): Api {
  const notMocked = (name: string) =>
    vi.fn(() => Promise.reject(new Error(`${name} not mocked in this test`)));
  const base: Api = {
    listApprovals: notMocked("listApprovals"),
    approve: notMocked("approve"),
    reject: notMocked("reject"),
    audit: notMocked("audit"),
    rulesFired: notMocked("rulesFired"),
    policies: notMocked("policies"),
    policy: notMocked("policy"),
    evals: notMocked("evals"),
    latestEval: notMocked("latestEval"),
    scenarios: notMocked("scenarios"),
    createSession: notMocked("createSession"),
    session: notMocked("session"),
    send: notMocked("send"),
    resume: notMocked("resume"),
    advance: notMocked("advance"),
    closeSession: notMocked("closeSession"),
  };
  return { ...base, ...overrides };
}

export function approval(overrides: Partial<Approval> = {}): Approval {
  return {
    id: "apr_1",
    session_id: "web-demo-1",
    tool: "place_order",
    args: { symbol: "NVDA", side: "buy", notional: 1500 },
    rule_id: "approval_notional",
    reason: "order notional $1,500 is over the $1,000 approval threshold",
    status: "pending",
    created_at: "2026-10-18T15:00:00+00:00",
    expires_at: "2099-10-18T15:05:00+00:00",
    decided_at: null,
    decided_by: null,
    note: null,
    result: null,
    audit_event_id: 7,
    ...overrides,
  };
}

export function auditEvent(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    id: 1,
    created_at: "2026-10-18T15:00:00+00:00",
    session_id: "web-demo-1",
    upstream: "replay",
    tool: "place_order",
    args: { symbol: "NVDA", side: "buy", notional: 3000 },
    decision: "DENY",
    rule_id: "max_order_notional",
    reason: "order notional $3,000 is over the $2,000 limit",
    latency_ms: 2.5,
    upstream_ms: 2.0,
    overhead_ms: 0.5,
    is_error: true,
    result_summary: null,
    trace_id: "ab".repeat(16),
    span_id: "cd".repeat(8),
    trace_url: `http://localhost:16686/trace/${"ab".repeat(16)}`,
    ...overrides,
  };
}
