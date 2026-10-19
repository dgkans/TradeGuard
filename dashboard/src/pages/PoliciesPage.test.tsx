import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiContext } from "../api";
import { fakeApi } from "../test/fakeApi";
import type { PolicyDetail } from "../types";
import { PoliciesPage } from "./PoliciesPage";

const standard: PolicyDetail = {
  name: "default", path: "policies/default.yaml", yaml: "name: default\nmax_order_notional_usd: 2000\n",
  policy: {
    name: "default", allowed_symbols: ["NVDA", "AMD", "AAPL"], allowed_order_types: ["market", "limit"],
    max_order_notional_usd: 2000, max_position_notional_usd: 5000, max_portfolio_exposure_pct: 60,
    max_orders_per_day: 10, market_hours_only: true, allow_short: false,
    require_approval: { notional_over_usd: 1000, any_sell_over_pct_of_position: 50 },
    taint: { enabled: true }, intent: { enabled: true, amount_tolerance_pct: 10 },
  },
};

const strict: PolicyDetail = {
  ...standard, name: "strict", path: "policies/strict.yaml", yaml: "name: strict\n",
  policy: { ...standard.policy, name: "strict", allowed_order_types: ["limit"],
            max_order_notional_usd: 500,
            require_approval: { notional_over_usd: 250, any_sell_over_pct_of_position: 25 } },
};

function api() {
  return fakeApi({
    policies: vi.fn(async () => [
      { name: "default", path: "policies/default.yaml" },
      { name: "rules_only", path: "policies/rules_only.yaml" },
      { name: "strict", path: "policies/strict.yaml" },
    ]),
    policy: vi.fn(async (name: string) => (name === "strict" ? strict : standard)),
    rulesFired: vi.fn(async () => [
      { rule_id: "max_order_notional", decision: "DENY" as const, count: 4,
        title: "Order is over your per-order limit" },
      { rule_id: "approval_notional", decision: "ESCALATE" as const, count: 2,
        title: "Large order: needs your approval" },
    ]),
  });
}

describe("PoliciesPage", () => {
  it("explains a risk profile in plain words, with the raw file behind a toggle", async () => {
    render(
      <ApiContext.Provider value={api()}>
        <PoliciesPage />
      </ApiContext.Provider>,
    );
    const tabs = await screen.findAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Standard", "Strict"]); // test-only file hidden
    const rules = await screen.findByRole("list", { name: /rules/i });
    for (const text of [/up to \$2,000 per order/i, /up to \$5,000 in any one stock/i,
                        /at most 60% of your account/i, /up to 10 orders a day/i,
                        /only while the market is open/i, /no short selling/i,
                        /orders over \$1,000 need your approval/i,
                        /selling more than 50% of a holding needs your approval/i,
                        /orders you didn't ask for are blocked/i,
                        /must match what you asked/i]) {
      expect(within(rules).getByText(text)).toBeInTheDocument();
    }
    expect(screen.getByText("NVDA")).toBeInTheDocument();
    expect(screen.queryByText(/max_order_notional_usd/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /show the policy file/i }));
    expect(screen.getByText(/max_order_notional_usd: 2000/)).toBeInTheDocument();
  });

  it("switches profiles and shows what the guard stopped in plain words", async () => {
    render(
      <ApiContext.Provider value={api()}>
        <PoliciesPage />
      </ApiContext.Provider>,
    );
    await userEvent.click(await screen.findByRole("tab", { name: "Strict" }));
    expect(await screen.findByText(/up to \$500 per order/i)).toBeInTheDocument();
    expect(screen.getByText(/limit orders only/i)).toBeInTheDocument();
    const stopped = screen.getByRole("region", { name: /what the guard stopped/i });
    expect(within(stopped).getByText("Order is over your per-order limit")).toBeInTheDocument();
    expect(within(stopped).getByText("4")).toBeInTheDocument();
    expect(within(stopped).queryByText("max_order_notional")).not.toBeInTheDocument();
  });

  it("counts only trader sessions (not test or eval runs)", async () => {
    const fake = api();
    render(
      <ApiContext.Provider value={fake}>
        <PoliciesPage />
      </ApiContext.Provider>,
    );
    await screen.findByRole("region", { name: /what the guard stopped/i });
    expect(fake.rulesFired).toHaveBeenCalledWith("traders");
  });
});
