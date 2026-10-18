import { createContext, useContext } from "react";
import type {
  Approval,
  AuditFilters,
  AuditPage,
  EvalReport,
  EvalReportRef,
  JsonObject,
  PolicyDetail,
  PolicyRef,
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
  rulesFired(): Promise<RuleCount[]>;
  policies(): Promise<PolicyRef[]>;
  policy(name: string): Promise<PolicyDetail>;
  evals(): Promise<EvalReportRef[]>;
  latestEval(): Promise<{ name: string; report: EvalReport }>;
  scenarios(): Promise<Scenario[]>;
  createSession(scenario: string, guarded: boolean, policy: string): Promise<SessionInfo>;
  session(id: string): Promise<SessionState>;
  send(id: string, text: string): Promise<TurnResult>;
  resume(id: string): Promise<TurnResult>;
  advance(id: string, minutes: number): Promise<{ clock: JsonObject }>;
  closeSession(id: string): Promise<void>;
}

const BASE: string = import.meta.env.VITE_API_URL ?? "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...init?.headers },
  });
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
  async rulesFired() {
    return (await request<{ rules: RuleCount[] }>("/rules/fired")).rules;
  },
  async policies() {
    return (await request<{ policies: PolicyRef[] }>("/policies")).policies;
  },
  policy: (name) => request<PolicyDetail>(`/policies/${encodeURIComponent(name)}`),
  async evals() {
    return (await request<{ reports: EvalReportRef[] }>("/evals")).reports;
  },
  latestEval: () => request<{ name: string; report: EvalReport }>("/evals/latest"),
  async scenarios() {
    return (await request<{ scenarios: Scenario[] }>("/scenarios")).scenarios;
  },
  createSession: (scenario, guarded, policy) =>
    post<SessionInfo>("/chat/sessions", { scenario, guarded, policy }),
  session: (id) => request<SessionState>(`/chat/sessions/${encodeURIComponent(id)}`),
  send: (id, text) => post<TurnResult>(`/chat/sessions/${encodeURIComponent(id)}/messages`, { text }),
  resume: (id) => post<TurnResult>(`/chat/sessions/${encodeURIComponent(id)}/resume`),
  advance: (id, minutes) =>
    post<{ clock: JsonObject }>(`/chat/sessions/${encodeURIComponent(id)}/advance`, { minutes }),
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
