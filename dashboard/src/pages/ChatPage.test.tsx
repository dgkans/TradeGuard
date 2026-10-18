import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiContext, type Api } from "../api";
import { approval, fakeApi } from "../test/fakeApi";
import type { Scenario, SessionInfo, TurnResult } from "../types";
import { ChatPage } from "./ChatPage";

const scenarios: Scenario[] = [
  { id: "demo", category: "demo", description: "Demo day", watchlist: ["NVDA", "AMD"],
    start_time: "2026-09-15T14:30:00+00:00", path: "scenarios/demo.yaml" },
  { id: "inj_001", category: "injection", description: "News says sell", watchlist: ["NVDA"],
    start_time: "2026-09-15T14:30:00+00:00", path: "scenarios/injection/inj_001.yaml" },
];

const info: SessionInfo = { session_id: "web-demo-1", scenario: "demo", guarded: true,
                            policy: "default", model: "ollama:qwen3:8b" };

function turn(overrides: Partial<TurnResult>): TurnResult {
  return { answer: null, pending_approval: null, understand: { request_type: "trade", summary: "" },
           tool_calls: [], outcome: null, ...overrides };
}

function renderWith(api: Api) {
  return render(
    <ApiContext.Provider value={api}>
      <ChatPage />
    </ApiContext.Provider>,
  );
}

describe("ChatPage", () => {
  it("starts a guarded session and shows tool calls and the answer", async () => {
    const createSession = vi.fn(async () => info);
    const send = vi.fn(async () => turn({
      answer: "NVDA is at $182.10.",
      tool_calls: [{ role: "tool", tool: "get_quote", args: { symbol: "NVDA" }, error: null,
                     latency_ms: 3, result_preview: '{"last": 182.1}' }],
    }));
    renderWith(fakeApi({ scenarios: vi.fn(async () => scenarios), createSession, send }));

    await userEvent.selectOptions(await screen.findByLabelText(/scenario/i), "inj_001");
    await userEvent.click(screen.getByRole("button", { name: /start session/i }));
    expect(createSession).toHaveBeenCalledWith("inj_001", true, "default");

    await userEvent.type(await screen.findByLabelText(/message/i), "What is NVDA at?");
    await userEvent.click(screen.getByRole("button", { name: /send/i }));
    expect(send).toHaveBeenCalledWith("web-demo-1", "What is NVDA at?");
    expect(await screen.findByText("NVDA is at $182.10.")).toBeInTheDocument();
    expect(screen.getByText("get_quote")).toBeInTheDocument();
    expect(screen.getByText("What is NVDA at?")).toBeInTheDocument();
  });

  it("can start an unguarded (no_guard) session", async () => {
    const createSession = vi.fn(async () => ({ ...info, guarded: false, policy: null }));
    renderWith(fakeApi({ scenarios: vi.fn(async () => scenarios), createSession }));
    await userEvent.click(await screen.findByLabelText(/no guard/i));
    await userEvent.click(screen.getByRole("button", { name: /start session/i }));
    expect(createSession).toHaveBeenCalledWith("demo", false, "default");
    expect(await screen.findByText(/no guard/i, { selector: ".session-mode" })).toBeInTheDocument();
  });

  it("pauses on an escalation, approves inline, then shows the finished turn", async () => {
    const pending = { approval_id: "apr_1", tool: "place_order", status: "pending",
                      args: { symbol: "NVDA", side: "buy", notional: 1500 },
                      rule_id: "approval_notional", reason: "over the $1,000 approval threshold" };
    const send = vi.fn(async () => turn({ pending_approval: pending }));
    // Like the server: /resume blocks until a human decides.
    let release: () => void = () => undefined;
    const decided = new Promise<void>((resolve) => { release = resolve; });
    const approve = vi.fn(async () => {
      release();
      return approval({ status: "approved" });
    });
    const resume = vi.fn(async () => {
      await decided;
      return turn({
        answer: "Bought $1,500 of NVDA.",
        outcome: { approval_id: "apr_1", status: "executed", executed: true,
                   decided_by: "operator" },
      });
    });
    renderWith(fakeApi({ scenarios: vi.fn(async () => scenarios),
                         createSession: vi.fn(async () => info), send, approve, resume }));
    await userEvent.click(await screen.findByRole("button", { name: /start session/i }));
    await userEvent.type(await screen.findByLabelText(/message/i), "Buy $1500 of NVDA");
    await userEvent.click(screen.getByRole("button", { name: /send/i }));

    const card = await screen.findByRole("article", { name: /approval needed/i });
    expect(within(card).getByText("approval_notional")).toBeInTheDocument();
    expect(within(card).getByText(/\$1,500/)).toBeInTheDocument();
    expect(resume).toHaveBeenCalledWith("web-demo-1"); // already waiting on the decision
    await userEvent.click(within(card).getByRole("button", { name: /approve/i }));
    expect(approve).toHaveBeenCalledWith("apr_1", "operator", "");
    expect(await screen.findByText("Bought $1,500 of NVDA.")).toBeInTheDocument();
    expect(screen.getByText(/executed/)).toBeInTheDocument();
  });
});
