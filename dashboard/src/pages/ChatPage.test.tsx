import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiContext, type Api } from "../api";
import { approval, fakeApi } from "../test/fakeApi";
import type { SessionInfo, SessionState, TurnResult } from "../types";
import { ChatPage } from "./ChatPage";

const info: SessionInfo = { session_id: "web-asha-1", user: "Asha", scenario: "desk",
                            guarded: true, policy: "default", model: "ollama:qwen3:8b",
                            started_at: "2026-10-18T15:00:00Z", busy: false,
                            waiting_for_approval: false };

function state(overrides: Partial<SessionState> = {}): SessionState {
  return { ...info, pending_approval: null, transcript: [], ...overrides };
}

function turn(overrides: Partial<TurnResult>): TurnResult {
  return { answer: null, pending_approval: null, steps: [], decision: null, ...overrides };
}

function renderWith(api: Api) {
  return render(
    <ApiContext.Provider value={api}>
      <ChatPage session={info} />
    </ApiContext.Provider>,
  );
}

async function say(text: string) {
  await userEvent.type(await screen.findByLabelText(/message/i), text);
  await userEvent.click(screen.getByRole("button", { name: /send/i }));
}

describe("ChatPage", () => {
  it("shows plain sentences and a formatted answer, never JSON", async () => {
    const send = vi.fn(async () => turn({
      answer: "NVDA is at **$212.74**.\n\n- up 1% today\n- volume normal",
      steps: [{ role: "step", status: "ok", tool: "get_quote", text: "Checked NVDA: $212.74" }],
    }));
    const { container } = renderWith(fakeApi({ session: vi.fn(async () => state()), send }));
    await say("What is NVDA at?");
    expect(send).toHaveBeenCalledWith("web-asha-1", "What is NVDA at?");
    expect(await screen.findByText("Checked NVDA: $212.74")).toBeInTheDocument();
    expect(screen.getByText("$212.74", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("up 1% today", { selector: "li" })).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/[{}]|"\w+":/);
    expect(container.textContent).not.toContain("**");
  });

  it("shows a blocked order as a notice with the reason", async () => {
    const send = vi.fn(async () => turn({
      answer: "I couldn't place that order.",
      steps: [{ role: "step", status: "blocked", tool: "place_order",
                text: "Blocked: buy $3,000 of TSLA at market",
                title: "Order is over your per-order limit",
                reason: "order size $3000.00 is over the limit $2000.00",
                rule_id: "max_order_notional" }],
    }));
    renderWith(fakeApi({ session: vi.fn(async () => state()), send }));
    await say("Buy $3000 of TSLA");
    const notice = await screen.findByRole("status", { name: /blocked/i });
    expect(within(notice).getByText("Order is over your per-order limit")).toBeInTheDocument();
    expect(within(notice).getByText("Blocked: buy $3,000 of TSLA at market")).toBeInTheDocument();
    expect(within(notice).getByText(/over the limit \$2000/)).toBeInTheDocument();
  });

  it("asks for approval, approves in the chat, then shows the result", async () => {
    const card = { approval_id: "apr_1", tool: "place_order", rule_id: "approval_notional",
                   args: { symbol: "NVDA", side: "buy", notional: 1500 },
                   title: "Large order: needs your approval",
                   reason: "order size $1500.00 is over the approval threshold $1000.00" };
    const send = vi.fn(async () => turn({ pending_approval: card }));
    let release: () => void = () => undefined;
    const decided = new Promise<void>((resolve) => { release = resolve; });
    const approve = vi.fn(async () => {
      release();
      return approval({ status: "approved" });
    });
    const resume = vi.fn(async () => {
      await decided;
      return turn({ answer: "Done: bought $1,500 of NVDA.",
                    decision: { role: "decision", status: "executed",
                                text: "Approved. The order was sent. Decided by Asha." } });
    });
    renderWith(fakeApi({ session: vi.fn(async () => state()), send, approve, resume }));
    await say("Buy $1500 of NVDA");

    const box = await screen.findByRole("article", { name: /needs your approval/i });
    expect(within(box).getByText("buy $1,500 of NVDA")).toBeInTheDocument();
    expect(within(box).getByText(/over the approval threshold/)).toBeInTheDocument();
    expect(resume).toHaveBeenCalledWith("web-asha-1");
    await userEvent.click(within(box).getByRole("button", { name: /approve/i }));
    expect(approve).toHaveBeenCalledWith("apr_1", "Asha", "");
    expect(await screen.findByText("Done: bought $1,500 of NVDA.")).toBeInTheDocument();
    expect(screen.getByText(/Approved\. The order was sent\./)).toBeInTheDocument();
  });

  it("restores the conversation when the trader comes back to it", async () => {
    const saved = state({ transcript: [
      { role: "user", text: "What is AMD at?" },
      { role: "step", status: "ok", tool: "get_quote", text: "Checked AMD: $160.10" },
      { role: "agent", text: "AMD is at $160.10." },
    ] });
    renderWith(fakeApi({ session: vi.fn(async () => saved) }));
    expect(await screen.findByText("AMD is at $160.10.")).toBeInTheDocument();
    expect(screen.getByText("Checked AMD: $160.10")).toBeInTheDocument();
  });

  it("has no clock buttons", async () => {
    renderWith(fakeApi({ session: vi.fn(async () => state()) }));
    await screen.findByLabelText(/message/i);
    expect(screen.queryByRole("button", { name: /min|hour/i })).not.toBeInTheDocument();
  });
});
