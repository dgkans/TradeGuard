import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiContext } from "../api";
import { fakeApi } from "../test/fakeApi";
import type { MarketOverview as Overview } from "../types";
import { MarketOverview, MarketWatch } from "./MarketOverview";

const spark = (start: number, end: number) =>
  Array.from({ length: 30 }, (_, i) => start + ((end - start) * i) / 29);

const data: Overview = {
  as_of: "2026-09-15T14:30:00+00:00", market_open: true,
  indices: [
    { symbol: "SPY", price: 757.47, previous_close: 760.9, day_change_pct: -0.45, spark: spark(760, 757.47) },
    { symbol: "QQQ", price: 706.55, previous_close: 709.2, day_change_pct: -0.37, spark: spark(709, 706.55) },
  ],
  watchlist: [
    { symbol: "NVDA", price: 212.74, previous_close: 210.96, day_change_pct: 0.84, spark: spark(210, 212.74) },
    { symbol: "AMD", price: 508.2, previous_close: 493.4, day_change_pct: 3.0, spark: spark(493, 508.2) },
  ],
  movers: [{ symbol: "AMD", day_change_pct: 3.0 }, { symbol: "NVDA", day_change_pct: 0.84 }],
};

describe("MarketOverview", () => {
  it("names the live source and the price time", async () => {
    const live = { ...data, live: true, source: "Yahoo Finance",
                   prices_as_of: "2026-10-16T20:00:00+00:00", market_open: false };
    render(
      <ApiContext.Provider value={fakeApi({ marketOverview: vi.fn(async () => live) })}>
        <MarketOverview />
      </ApiContext.Provider>,
    );
    expect(await screen.findByText(/Market closed · live prices from Yahoo Finance, as of .*16:00 ET/))
      .toBeInTheDocument();
  });

  it("shows index and stock cards with prices, day change and a sparkline", async () => {
    render(
      <ApiContext.Provider value={fakeApi({ marketOverview: vi.fn(async () => data) })}>
        <MarketOverview />
      </ApiContext.Provider>,
    );
    const spy = await screen.findByRole("article", { name: "SPY" });
    expect(within(spy).getByText("757.47")).toBeInTheDocument();
    expect(within(spy).getByText("-0.45%")).toHaveClass("down");
    expect(within(spy).getByRole("img", { name: /SPY price today/i })).toBeInTheDocument();
    const amd = screen.getByRole("article", { name: "AMD" });
    expect(within(amd).getByText("+3.00%")).toHaveClass("up");
    expect(screen.getByRole("img", { name: /top movers/i })).toBeInTheDocument();
    expect(screen.getByText(/10:30 ET/)).toBeInTheDocument();
  });

  it("has a compact market watch for beside the chat", async () => {
    render(
      <ApiContext.Provider value={fakeApi({ marketOverview: vi.fn(async () => data) })}>
        <MarketWatch />
      </ApiContext.Provider>,
    );
    const list = await screen.findByRole("list", { name: /market watch/i });
    expect(within(list).getAllByRole("listitem")).toHaveLength(4);
    expect(within(list).getByText("NVDA")).toBeInTheDocument();
  });
});
