// Shapes returned by the TradeGuard API (services/api/app.py).

export type Decision = "ALLOW" | "DENY" | "ESCALATE";

export type ApprovalStatus =
  | "pending"
  | "approved"
  | "executing"
  | "executed"
  | "blocked"
  | "rejected"
  | "expired";

export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };

export interface Approval {
  id: string;
  session_id: string;
  tool: string;
  args: JsonObject;
  rule_id: string;
  reason: string;
  status: ApprovalStatus;
  created_at: string;
  expires_at: string;
  decided_at: string | null;
  decided_by: string | null;
  note: string | null;
  result: JsonObject | null;
  audit_event_id: number | null;
}

export interface AuditEvent {
  id: number;
  created_at: string | null;
  session_id: string;
  upstream: string | null;
  tool: string;
  args: JsonObject;
  decision: Decision;
  rule_id: string;
  reason: string;
  latency_ms: number | null;
  upstream_ms: number | null;
  overhead_ms: number | null;
  is_error: boolean | null;
  result_summary: string | null;
  trace_id: string | null;
  span_id: string | null;
  trace_url: string | null;
}

export interface AuditPage {
  events: AuditEvent[];
  next_before_id: number | null;
}

export interface AuditFilters {
  session_id?: string;
  decision?: Decision;
  tool?: string;
  before_id?: number;
  limit?: number;
}

export interface RuleCount {
  rule_id: string;
  decision: Decision;
  count: number;
}

export interface PolicyRef {
  name: string;
  path: string;
}

export interface PolicyDetail extends PolicyRef {
  yaml: string;
  policy: JsonObject;
}

export interface MetricCell {
  mean: number | null;
  min: number | null;
  max: number | null;
  repeats: number;
}

export type ConfigMetrics = Record<string, MetricCell>;

export interface JudgeSummary {
  model: string;
  prompt_version: string;
  date: string;
  cases: number;
  agreed: number;
  agreement_on_cases_gateway_sends_to_judge: { cases: number; agreed: number };
}

export interface DetectorSummary {
  date: string;
  snippets: number;
  precision: number;
  recall: number;
}

export interface EvalReport {
  header: Record<string, Json>;
  tuning: Record<string, ConfigMetrics>;
  held_out: Record<string, ConfigMetrics>;
  by_category: Record<string, Record<string, number>>;
  judge?: JudgeSummary | null;
  detector?: DetectorSummary | null;
}

export interface EvalReportRef {
  name: string;
  date: string | null;
  commit: string | null;
  agent_model: string | null;
  smoke: boolean;
  mode: string | null;
  repeats: number | null;
  suite: string;
}

export interface Scenario {
  id: string;
  category: string;
  description: string;
  watchlist: string[];
  start_time: string;
  path: string;
}

export interface SessionInfo {
  session_id: string;
  scenario: string;
  guarded: boolean;
  policy: string | null;
  model: string;
}

export interface PendingApproval {
  approval_id: string;
  tool: string;
  args?: JsonObject;
  rule_id: string;
  reason: string;
  expires_at?: string;
  status: string;
}

export interface ApprovalOutcome {
  approval_id: string;
  status: string;
  executed?: boolean;
  decided_by?: string | null;
  note?: string | null;
  reason?: string;
  [key: string]: Json | undefined;
}

export interface ToolCallEntry {
  role: "tool";
  tool: string;
  args: JsonObject;
  error: string | null;
  latency_ms: number;
  result_preview: string | null;
}

export type TranscriptEntry =
  | { role: "user"; text: string }
  | { role: "agent"; text: string }
  | ToolCallEntry
  | ({ role: "approval" } & PendingApproval)
  | { role: "decision"; outcome: ApprovalOutcome }
  | { role: "clock"; minutes: number; clock: JsonObject };

export interface SessionState extends SessionInfo {
  busy: boolean;
  pending_approval: PendingApproval | null;
  transcript: TranscriptEntry[];
}

export interface TurnResult {
  answer: string | null;
  pending_approval: PendingApproval | null;
  understand: { request_type: string | null; summary: string | null };
  tool_calls: ToolCallEntry[];
  outcome: ApprovalOutcome | null;
}
