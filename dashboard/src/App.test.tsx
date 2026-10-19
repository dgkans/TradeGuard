import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { ApiContext, ApiError } from "./api";
import { fakeApi } from "./test/fakeApi";
import type { MarketOverview, Me } from "./types";

const me: Me = {
  trader: { id: 1, username: "asha", display_name: "Asha", profile: "default",
            starting_cash: 10000 },
  session: { session_id: "web-1-asha", user: "Asha", scenario: "desk", guarded: true,
             policy: "default", model: "m", started_at: "2026-10-18T15:00:00Z", busy: false,
             waiting_for_approval: false },
};

function renderApp(overrides = {}) {
  let admin = false;
  const api = fakeApi({
    marketOverview: vi.fn(() => new Promise<MarketOverview>(() => undefined)),
    marketStatus: vi.fn(async () => ({
      live: true, source: "Yahoo Finance", now: "2026-10-18T16:00:00Z", market_open: false,
      session_start: "2026-10-19T13:30:00Z", session_end: "2026-10-19T20:00:00Z",
      prices_as_of: "2026-10-16T20:00:00Z", updated_at: "2026-10-18T15:59:30Z", stale: false,
    })),
    listApprovals: vi.fn(async () => []),
    traders: vi.fn(async () => []),
    isAdmin: vi.fn(() => admin),
    login: vi.fn(async (username: string, password: string) => {
      if (username !== "admin" || password !== "secret") {
        throw new ApiError(401, "wrong username or password");
      }
      admin = true;
      return "admin" as const;
    }),
    adminLogout: vi.fn(() => {
      admin = false;
    }),
    ...overrides,
  });
  render(
    <ApiContext.Provider value={api}>
      <App />
    </ApiContext.Provider>,
  );
  return api;
}

describe("App", () => {
  it("shows visitors the trader pages, the landing page and the live market status", async () => {
    renderApp();
    const nav = screen.getByRole("navigation", { name: /pages/i });
    expect(nav).toHaveTextContent(/Chat & Trade/);
    expect(nav).toHaveTextContent(/Positions/);
    expect(nav).toHaveTextContent(/My limits/);
    for (const hidden of [/Traders/, /Approvals/, /Audit log/, /Test results/]) {
      expect(nav).not.toHaveTextContent(hidden);
    }
    expect(await screen.findByText(/Live · Market closed · opens Mon 9:30 AM ET/))
      .toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^admin$/i })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /trade in plain words/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /sign up/i })).toBeInTheDocument();
  });

  it("opens the trader's desk once logged in", async () => {
    let trader = false;
    const api = renderApp({
      isTrader: vi.fn(() => trader),
      login: vi.fn(async () => {
        trader = true;
        return "trader" as const;
      }),
      me: vi.fn(async () => me),
      session: vi.fn(() => new Promise(() => undefined)),
    });
    await userEvent.type(screen.getByLabelText(/username/i), "asha");
    await userEvent.type(screen.getByLabelText(/password/i), "a-long-password");
    await userEvent.click(screen.getByRole("button", { name: /^log in$/i }));
    expect(api.login).toHaveBeenCalledWith("asha", "a-long-password");
    expect(await screen.findByRole("heading", { name: "Asha" })).toBeInTheDocument();
    expect(screen.getByText("@asha")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /trade in plain words/i }))
      .not.toBeInTheDocument();
  });

  it("opens the admin views when the owner logs in as admin, and closes them on log out", async () => {
    const api = renderApp();
    await userEvent.type(screen.getByLabelText(/username/i), "admin");
    await userEvent.type(screen.getByLabelText(/password/i), "wrong");
    await userEvent.click(screen.getByRole("button", { name: /^log in$/i }));
    expect(await screen.findByText(/wrong username or password/)).toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText(/password/i));
    await userEvent.type(screen.getByLabelText(/password/i), "secret");
    await userEvent.click(screen.getByRole("button", { name: /^log in$/i }));
    const nav = screen.getByRole("navigation", { name: /pages/i });
    expect(await screen.findByText("Audit log")).toBeInTheDocument();
    expect(nav).toHaveTextContent(/Traders/);
    expect(nav).toHaveTextContent(/Test results/);
    expect(nav).not.toHaveTextContent(/Chat & Trade/);  // the owner has no trading account
    expect(screen.getByText(/Owner view/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /log out/i }));
    expect(api.adminLogout).toHaveBeenCalled();
    expect(nav).not.toHaveTextContent(/Audit log/);
    expect(nav).toHaveTextContent(/Chat & Trade/);
  });
});
