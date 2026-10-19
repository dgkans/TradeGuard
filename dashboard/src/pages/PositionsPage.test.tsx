import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiContext } from "../api";
import { fakeApi } from "../test/fakeApi";
import type { Portfolio, SessionInfo } from "../types";
import { PositionsPage } from "./PositionsPage";

const info: SessionInfo = { session_id: "web-asha-1", user: "Asha", scenario: "desk",
                            guarded: true, policy: "default", model: "m",
                            started_at: "2026-10-18T15:00:00Z", busy: false,
                            waiting_for_approval: false };

const book: Portfolio = {
  as_of: "2026-09-15T14:40:00+00:00", market_open: true, cash: 23500, equity: 28800,
  positions_value: 5300,
  positions: [
    { symbol: "NVDA", qty: 17.05, avg: 212.74, ltp: 214.1, value: 3650.4, pnl: 23.19,
      pnl_pct: 0.64, day_change_pct: 1.2 },
    { symbol: "AAPL", qty: 5, avg: 329.45, ltp: 328.0, value: 1640, pnl: -7.25,
      pnl_pct: -0.44, day_change_pct: -0.5 },
  ],
  total_pnl: 15.94, total_day_change: 35.5,
  orders: [
    { id: "ord_1", symbol: "NVDA", side: "buy", type: "market", qty: null, notional: 1500,
      limit_price: null, status: "filled", submitted_at: "2026-09-15T14:30:00+00:00",
      filled_qty: 7.05, filled_avg_price: 212.77 },
  ],
};

describe("PositionsPage", () => {
  it("shows each position with qty, avg, LTP, P&L and day change, and the totals", async () => {
    const portfolio = vi.fn(async () => book);
    render(
      <ApiContext.Provider value={fakeApi({ portfolio })}>
        <PositionsPage session={info} pollMs={0} />
      </ApiContext.Provider>,
    );
    const table = await screen.findByRole("table", { name: /positions/i });
    const header = within(table).getAllByRole("columnheader").map((cell) => cell.textContent);
    expect(header).toEqual(["Instrument", "Qty.", "Avg.", "LTP", "P&L", "Chg."]);
    const nvda = within(table).getByRole("row", { name: /NVDA/ });
    expect(within(nvda).getByText("17.05")).toBeInTheDocument();
    expect(within(nvda).getByText("212.74")).toBeInTheDocument();
    expect(within(nvda).getByText("214.10")).toBeInTheDocument();
    expect(within(nvda).getByText("+23.19")).toHaveClass("up");
    expect(within(nvda).getByText("+1.20%")).toHaveClass("up");
    const aapl = within(table).getByRole("row", { name: /AAPL/ });
    expect(within(aapl).getByText("-7.25")).toHaveClass("down");
    const total = within(table).getByRole("row", { name: /total/i });
    expect(within(total).getByText("+15.94")).toHaveClass("up");

    expect(screen.getByText("$23,500")).toBeInTheDocument(); // cash card
    const orders = screen.getByRole("table", { name: /orders/i });
    expect(within(orders).getByText("BUY")).toBeInTheDocument();
    expect(within(orders).getByText("$1,500")).toBeInTheDocument();
    expect(within(orders).getByText("filled")).toBeInTheDocument();
    expect(portfolio).toHaveBeenCalledWith("web-asha-1");
  });

  it("says so when the trader has no positions yet", async () => {
    const empty = { ...book, positions: [], orders: [], total_pnl: 0, total_day_change: 0 };
    render(
      <ApiContext.Provider value={fakeApi({ portfolio: vi.fn(async () => empty) })}>
        <PositionsPage session={info} pollMs={0} />
      </ApiContext.Provider>,
    );
    expect(await screen.findByText(/no open positions/i)).toBeInTheDocument();
  });

  it("shows a tiny P&L as a neutral 0.00 and market times in New York time", async () => {
    const flat = { ...book, total_pnl: -0.004, positions: [
      { symbol: "AMD", qty: 0.98, avg: 507.88, ltp: 507.88, value: 497.7, pnl: -0.004,
        pnl_pct: 0, day_change_pct: 0 },
    ] };
    render(
      <ApiContext.Provider value={fakeApi({ portfolio: vi.fn(async () => flat) })}>
        <PositionsPage session={info} pollMs={0} />
      </ApiContext.Provider>,
    );
    const table = await screen.findByRole("table", { name: /positions/i });
    const amd = within(table).getByRole("row", { name: /AMD/ });
    const pnl = within(amd).getAllByText("0.00")[0] as HTMLElement;
    expect(pnl).not.toHaveClass("down");
    expect(within(amd).queryByText("-0.00")).not.toBeInTheDocument();
    expect(screen.getByText(/10:40 ET/)).toBeInTheDocument(); // 14:40 UTC
  });
});

