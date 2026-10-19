import { createContext, useContext } from "react";
import type {
  Approval,
  AuditFilters,
  AuditPage,
  EvalReport,
  EvalReportRef,
  PolicyDetail,
  Limits,
  LimitsView,
  MarketOverview,
  PolicyRef,
  Portfolio,
  RuleCount,
  Scenario,
  SessionInfo,
  SessionState,
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
  sessions(): Promise<SessionInfo[]>;
  createSession(user: string, policy: string, startingCash: number): Promise<SessionInfo>;
  session(id: string): Promise<SessionState>;
  send(id: string, text: string): Promise<TurnResult>;
  resume(id: string): Promise<TurnResult>;
  portfolio(id: string): Promise<Portfolio>;
  closeSession(id: string): Promise<void>;
  /** A trader decides an approval in their own session. */
  approveOwn(sessionId: string, approvalId: string, note: string): Promise<Approval>;
  rejectOwn(sessionId: string, approvalId: string, note: string): Promise<Approval>;
  limits(sessionId: string, template?: string): Promise<LimitsView>;
  setLimits(sessionId: string, limits: Limits): Promise<LimitsView>;
  /** What the guard blocked or held for this trader. */
  stopped(sessionId: string): Promise<RuleCount[]>;
  marketOverview(): Promise<MarketOverview>;
  adminLogin(password: string): Promise<void>;
  adminLogout(): void;
  isAdmin(): boolean;
}

const TOKEN_KEY = "tradeguard.admin-token";

function adminToken(): string | null {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function setAdminToken(token: string | null) {
  try {
    if (token) window.sessionStorage.setItem(TOKEN_KEY, token);
    else window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // Storage blocked: admin stays logged in for this page only.
  }
}

const BASE: string = import.meta.env.VITE_API_URL ?? "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = adminToken();
  const auth: Record<string, string> = token ? { authorization: `Bearer ${token}` } : {};
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...auth, ...init?.headers },
  });
  if (response.status === 401 && token) {
    setAdminToken(null); // expired or the API restarted: back to the trader view
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
  async sessions() {
    return (await request<{ sessions: SessionInfo[] }>("/chat/sessions")).sessions;
  },
  createSession: (user, policy, startingCash) =>
    post<SessionInfo>("/chat/sessions", { user, policy, scenario: "desk", guarded: true,
                                          starting_cash: startingCash }),
  session: (id) => request<SessionState>(`/chat/sessions/${encodeURIComponent(id)}`),
  send: (id, text) => post<TurnResult>(`/chat/sessions/${encodeURIComponent(id)}/messages`, { text }),
  resume: (id) => post<TurnResult>(`/chat/sessions/${encodeURIComponent(id)}/resume`),
  portfolio: (id) => request<Portfolio>(`/chat/sessions/${encodeURIComponent(id)}/portfolio`),
  approveOwn: (sessionId, approvalId, note) =>
    post<Approval>(`/chat/sessions/${encodeURIComponent(sessionId)}/approvals/` +
                   `${encodeURIComponent(approvalId)}/approve`, { note }),
  rejectOwn: (sessionId, approvalId, note) =>
    post<Approval>(`/chat/sessions/${encodeURIComponent(sessionId)}/approvals/` +
                   `${encodeURIComponent(approvalId)}/reject`, { note }),
  limits: (sessionId, template) =>
    request<LimitsView>(`/chat/sessions/${encodeURIComponent(sessionId)}/limits` +
                        query({ template })),
  setLimits: (sessionId, limits) =>
    request<LimitsView>(`/chat/sessions/${encodeURIComponent(sessionId)}/limits`,
                        { method: "PUT", body: JSON.stringify(limits) }),
  async stopped(sessionId) {
    const data = await request<{ rules: RuleCount[] }>(
      `/chat/sessions/${encodeURIComponent(sessionId)}/stopped`);
    return data.rules;
  },
  marketOverview: () => request<MarketOverview>("/market/overview"),
  async adminLogin(password) {
    const data = await post<{ token: string }>("/admin/login", { password });
    setAdminToken(data.token);
  },
  adminLogout: () => setAdminToken(null),
  isAdmin: () => adminToken() !== null,
  async closeSession(id) {
    await request<unknown>(`/chat/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
  },
};

export const ApiContext = createContext<Api>(httpApi);

export function useApi(): Api {
  return useContext(ApiContext);
}

export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.message} (HTTP ${error.status})`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}
