import { createContext, useContext } from "react";
import type {
  AdminTrader,
  Approval,
  AuditFilters,
  AuditPage,
  EvalReport,
  EvalReportRef,
  Limits,
  LimitsView,
  MarketOverview,
  MarketStatus,
  Me,
  PolicyDetail,
  PolicyRef,
  Portfolio,
  Role,
  RuleCount,
  Scenario,
  SessionState,
  SignupForm,
  TurnResult,
} from "./types";

/** Error from the API, with the HTTP status and the server's `detail` message. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Everything the dashboard asks the backend. Pages get it from context, so tests can fake it. */
export interface Api {
  // --- trader account (F10e) ---
  signup(form: SignupForm): Promise<void>;
  /** Traders, and the owner as `admin` on the same form (F10f). */
  login(username: string, password: string): Promise<Role>;
  logout(): Promise<void>;
  isTrader(): boolean;
  /** The logged-in trader and their session (opened or restored on demand). */
  me(): Promise<Me>;
  session(): Promise<SessionState>;
  send(text: string): Promise<TurnResult>;
  resume(): Promise<TurnResult>;
  portfolio(): Promise<Portfolio>;
  approveOwn(approvalId: string, note: string): Promise<Approval>;
  rejectOwn(approvalId: string, note: string): Promise<Approval>;
  limits(template?: string): Promise<LimitsView>;
  setLimits(limits: Limits): Promise<LimitsView>;
  /** What the guard blocked or held for this trader. */
  stopped(): Promise<RuleCount[]>;
  // --- market (public) ---
  /** Resolves once the API answers (it sleeps when idle on the free host, F12). */
  health(): Promise<void>;
  marketOverview(): Promise<MarketOverview>;
  marketStatus(): Promise<MarketStatus>;
  // --- admin (owner only) ---
  adminLogout(): void;
  isAdmin(): boolean;
  traders(): Promise<AdminTrader[]>;
  listApprovals(status?: string): Promise<Approval[]>;
  approve(id: string, decidedBy: string, note: string): Promise<Approval>;
  reject(id: string, decidedBy: string, note: string): Promise<Approval>;
  audit(filters: AuditFilters): Promise<AuditPage>;
  /** Blocked and held counts per rule; "traders" counts dashboard sessions only. */
  rulesFired(scope?: "traders"): Promise<RuleCount[]>;
  policies(): Promise<PolicyRef[]>;
  policy(name: string): Promise<PolicyDetail>;
  evals(): Promise<EvalReportRef[]>;
  /** The latest LLM run, or with "attacker" the scripted worst case run. */
  latestEval(agent?: "attacker"): Promise<{ name: string; report: EvalReport }>;
  scenarios(): Promise<Scenario[]>;
}

// Admin: this tab only. Trader: remembered in this browser (the token expires in 7 days).
const ADMIN_KEY = "tradeguard.admin-token";
const TRADER_KEY = "tradeguard.trader-token";

function read(storage: () => Storage, key: string): string | null {
  try {
    return storage().getItem(key);
  } catch {
    return null;
  }
}

function write(storage: () => Storage, key: string, value: string | null) {
  try {
    if (value) storage().setItem(key, value);
    else storage().removeItem(key);
  } catch {
    // Storage blocked: the login lasts for this page only.
  }
}

const session = () => window.sessionStorage;
const local = () => window.localStorage;

const BASE: string = import.meta.env.VITE_API_URL ?? "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const admin = read(session, ADMIN_KEY);
  const trader = read(local, TRADER_KEY);
  const auth: Record<string, string> = {};
  if (admin) auth.authorization = `Bearer ${admin}`;
  if (trader) auth["x-trader-token"] = trader;
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...auth, ...init?.headers },
  });
  if (response.status === 401) {
    // Expired, or the API was reset: drop the stale login for the area that refused.
    if (path.startsWith("/me") || path.startsWith("/auth/logout")) write(local, TRADER_KEY, null);
    else if (admin) write(session, ADMIN_KEY, null);
  }
  if (response.status === 204) {
    return null as T;
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    throw new ApiError(response.status, detailOf(body) ?? `HTTP ${response.status}`);
  }
  return body as T;
}

function detailOf(body: unknown): string | null {
  if (typeof body !== "object" || body === null || !("detail" in body)) {
    return null;
  }
  const detail = (body as { detail: unknown }).detail;
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    // FastAPI validation errors: name the fields in plain words.
    const fields = detail.map((item) => {
      const loc = (item as { loc?: unknown[] }).loc ?? [];
      return String(loc[loc.length - 1] ?? "field").replace(/_/g, " ");
    });
    return `please check: ${[...new Set(fields)].join(", ")}`;
  }
  return JSON.stringify(detail);
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") {
      search.set(key, String(value));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export const httpApi: Api = {
  async signup(form) {
    const data = await post<{ token: string }>("/auth/signup", form);
    write(local, TRADER_KEY, data.token);
  },
  async login(username, password) {
    const data = await post<{ token: string; role: Role }>("/auth/login", { username, password });
    if (data.role === "admin") {
      write(session, ADMIN_KEY, data.token);
    } else {
      write(local, TRADER_KEY, data.token);
    }
    return data.role;
  },
  async logout() {
    try {
      await post<unknown>("/auth/logout");
    } finally {
      write(local, TRADER_KEY, null);
    }
  },
  isTrader: () => read(local, TRADER_KEY) !== null,
  me: () => request<Me>("/me"),
  session: () => request<SessionState>("/me/session"),
  send: (text) => post<TurnResult>("/me/messages", { text }),
  resume: () => post<TurnResult>("/me/resume"),
  portfolio: () => request<Portfolio>("/me/portfolio"),
  approveOwn: (approvalId, note) =>
    post<Approval>(`/me/approvals/${encodeURIComponent(approvalId)}/approve`, { note }),
  rejectOwn: (approvalId, note) =>
    post<Approval>(`/me/approvals/${encodeURIComponent(approvalId)}/reject`, { note }),
  limits: (template) => request<LimitsView>(`/me/limits${query({ template })}`),
  setLimits: (limits) =>
    request<LimitsView>("/me/limits", { method: "PUT", body: JSON.stringify(limits) }),
  async stopped() {
    return (await request<{ rules: RuleCount[] }>("/me/stopped")).rules;
  },
  async health() {
    await request<unknown>("/healthz");
  },
  marketOverview: () => request<MarketOverview>("/market/overview"),
  marketStatus: () => request<MarketStatus>("/market/status"),
  adminLogout: () => write(session, ADMIN_KEY, null),
  isAdmin: () => read(session, ADMIN_KEY) !== null,
  async traders() {
    return (await request<{ traders: AdminTrader[] }>("/admin/traders")).traders;
  },
  async listApprovals(status) {
    const data = await request<{ approvals: Approval[] }>(`/approvals${query({ status })}`);
    return data.approvals;
  },
  approve: (id, decidedBy, note) =>
    post<Approval>(`/approvals/${encodeURIComponent(id)}/approve`, { decided_by: decidedBy, note }),
  reject: (id, decidedBy, note) =>
    post<Approval>(`/approvals/${encodeURIComponent(id)}/reject`, { decided_by: decidedBy, note }),
  audit: (filters) => request<AuditPage>(`/audit${query({ ...filters })}`),
  async rulesFired(scope) {
    return (await request<{ rules: RuleCount[] }>(`/rules/fired${query({ scope })}`)).rules;
  },
  async policies() {
    return (await request<{ policies: PolicyRef[] }>("/policies")).policies;
  },
  policy: (name) => request<PolicyDetail>(`/policies/${encodeURIComponent(name)}`),
  async evals() {
    return (await request<{ reports: EvalReportRef[] }>("/evals")).reports;
  },
  latestEval: (agent) =>
    request<{ name: string; report: EvalReport }>(`/evals/latest${query({ agent })}`),
  async scenarios() {
    return (await request<{ scenarios: Scenario[] }>("/scenarios")).scenarios;
  },
};

export const ApiContext = createContext<Api>(httpApi);

export function useApi(): Api {
  return useContext(ApiContext);
}

export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}
