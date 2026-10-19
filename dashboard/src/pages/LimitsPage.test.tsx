import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiContext, ApiError } from "../api";
import { fakeApi } from "../test/fakeApi";
import type { LimitsView, SessionInfo } from "../types";
import { LimitsPage } from "./LimitsPage";

const info: SessionInfo = { session_id: "web-asha-1", user: "Asha", scenario: "desk",
                            guarded: true, policy: "default", model: "m",
                            started_at: "2026-10-18T15:00:00Z", busy: false,
                            waiting_for_approval: false };

const view: LimitsView = {
  profile: "default",
  limits: { max_order_usd: 2000, max_position_usd: 5000, max_exposure_pct: 60,
            max_orders_per_day: 10, approval_over_usd: 1000, approval_sell_pct: 50,
            allowed_symbols: ["NVDA", "AMD", "AAPL"] },
  locked: ["No short selling", "Only while the market is open",
           "Orders you didn't ask for are blocked", "Each order must match what you asked"],
  universe: ["AAPL", "AMD", "MSFT", "NVDA", "SPY"],
};

function setup(overrides = {}) {
  const api = fakeApi({
    limits: vi.fn(async (template?: string) =>
      template === "strict"
        ? { ...view, profile: "strict", limits: { ...view.limits, max_order_usd: 500,
                                                   approval_over_usd: 250,
                                                   allowed_symbols: ["AAPL", "MSFT", "SPY"] } }
        : view),
    setLimits: vi.fn(async (limits: LimitsView["limits"]) => ({ ...view, limits })),
    stopped: vi.fn(async () => [{ rule_id: "max_order_notional", decision: "DENY" as const,
                                  count: 2, title: "Order is over your per-order limit" }]),
    ...overrides,
  });
  render(
    <ApiContext.Provider value={api}>
      <LimitsPage session={info} />
    </ApiContext.Provider>,
  );
  return api;
}

describe("LimitsPage", () => {
  it("lets the trader change limits and saves them", async () => {
    const api = setup();
    const perOrder = await screen.findByLabelText(/per order/i);
    expect(perOrder).toHaveValue(2000);
    await userEvent.clear(perOrder);
    await userEvent.type(perOrder, "1500");
    await userEvent.click(screen.getByRole("checkbox", { name: "MSFT" }));
    await userEvent.click(screen.getByRole("button", { name: /save my limits/i }));
    expect(api.setLimits).toHaveBeenCalledWith({
      ...view.limits, max_order_usd: 1500, allowed_symbols: ["NVDA", "AMD", "AAPL", "MSFT"],
    });
    expect(await screen.findByText(/saved/i)).toBeInTheDocument();
  });

  it("shows the protections that can't be turned off, and what was stopped", async () => {
    setup();
    const locked = await screen.findByRole("list", { name: /always on/i });
    expect(within(locked).getAllByRole("listitem")).toHaveLength(4);
    expect(within(locked).getByText("Orders you didn't ask for are blocked")).toBeInTheDocument();
    const stopped = screen.getByRole("region", { name: /stopped for you/i });
    expect(within(stopped).getByText("Order is over your per-order limit")).toBeInTheDocument();
    expect(within(stopped).getByText("2")).toBeInTheDocument();
  });

  it("checks values before saving", async () => {
    const api = setup();
    const approval = await screen.findByLabelText(/approval over/i);
    await userEvent.clear(approval);
    await userEvent.type(approval, "3000");
    expect(screen.getByRole("button", { name: /save my limits/i })).toBeDisabled();
    expect(screen.getByText(/can't be more than your per-order limit/i)).toBeInTheDocument();
    expect(api.setLimits).not.toHaveBeenCalled();
  });

  it("fills the form from a profile template", async () => {
    setup();
    await screen.findByLabelText(/per order/i);
    await userEvent.click(screen.getByRole("button", { name: /start from strict/i }));
    expect(await screen.findByLabelText(/per order/i)).toHaveValue(500);
    expect(screen.getByRole("checkbox", { name: "NVDA" })).not.toBeChecked();
  });

  it("shows the server's reason when a save is refused", async () => {
    setup({ setLimits: vi.fn(() => Promise.reject(new ApiError(422, "allowed_symbols: bad"))) });
    await userEvent.click(await screen.findByRole("button", { name: /save my limits/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/allowed_symbols: bad/);
  });
});
