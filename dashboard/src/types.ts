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
  /** Plain-language title, e.g. "Order is over your per-order limit". */
  title?: string;
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
  agent?: string | null;
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
  user: string;
  scenario: string;
  guarded: boolean;
  policy: string | null;
  model: string;
  started_at: string;
  starting_cash?: number;
  busy: boolean;
  waiting_for_approval: boolean;
}

export type StepStatus = "ok" | "blocked" | "held" | "failed";

export interface StepEntry {
  role: "step";
  status: StepStatus;
  tool: string;
  text: string;
  title?: string;
  reason?: string;
  rule_id?: string;
}

export interface ApprovalCardData {
  approval_id: string;
  tool: string;
  args: JsonObject;
  rule_id: string;
  title: string;
  reason: string;
  expires_at?: string | null;
}

export interface DecisionEntry {
  role: "decision";
  status: string;
  text: string;
}

export type TranscriptEntry =
  | { role: "user"; text: string }
  | { role: "agent"; text: string }
  | StepEntry
  | ({ role: "approval" } & ApprovalCardData)
  | DecisionEntry;

export interface SessionState extends SessionInfo {
  pending_approval: ApprovalCardData | null;
  transcript: TranscriptEntry[];
}

export interface TurnResult {
  answer: string | null;
  pending_approval: ApprovalCardData | null;
  steps: StepEntry[];
  decision: DecisionEntry | null;
}

export interface PositionRow {
  symbol: string;
  qty: number;
  avg: number;
  ltp: number;
  value: number;
  pnl: number;
  pnl_pct: number;
  day_change_pct: number | null;
}

export interface OrderRow {
  id: string;
  symbol: string;
  side: string;
  type: string;
  qty: number | null;
  notional: number | null;
  limit_price: number | null;
  status: string;
  submitted_at: string;
  filled_qty: number | null;
  filled_avg_price: number | null;
}

export interface Portfolio {
  as_of: string;
  market_open: boolean;
  cash: number;
  equity: number;
  positions_value: number;
  positions: PositionRow[];
  total_pnl: number;
  total_day_change: number;
  orders: OrderRow[];
}

export interface Limits {
  max_order_usd: number;
  max_position_usd: number;
  max_exposure_pct: number;
  max_orders_per_day: number;
  approval_over_usd: number;
  approval_sell_pct: number;
  allowed_symbols: string[];
}

export interface LimitsView {
  profile: string;
  limits: Limits;
  locked: string[];
  universe: string[];
}

export interface MarketItem {
  symbol: string;
  price: number;
  previous_close: number | null;
  day_change_pct: number;
  spark: number[];
}

export interface MarketOverview {
  as_of: string;
  market_open: boolean;
  /** Live prices (F10f) or the replay fixtures. */
  live?: boolean;
  source?: string;
  prices_as_of?: string | null;
  indices: MarketItem[];
  watchlist: MarketItem[];
  movers: { symbol: string; day_change_pct: number }[];
}

export interface TraderInfo {
  id: number;
  username: string;
  display_name: string;
  profile: string;
  starting_cash: number;
}

export interface Me {
  trader: TraderInfo | null;
  session: SessionInfo;
}

export interface SignupForm {
  username: string;
  password: string;
  display_name?: string;
  starting_cash: number;
  profile: string;
}

/** Where prices come from and how fresh they are (F10f). */
export interface MarketStatus {
  live: boolean;
  source: string;
  now: string | null;
  market_open: boolean;
  session_start: string | null;
  session_end: string | null;
  prices_as_of: string | null;
  updated_at: string | null;
  stale: boolean;
}

export type Role = "trader" | "admin";

export interface AdminTrader extends TraderInfo {
  active: boolean;
  waiting_for_approval: boolean;
  orders: number;
}
