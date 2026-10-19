import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { ApiContext, ApiError } from "./api";
import { fakeApi } from "./test/fakeApi";
import type { MarketOverview } from "./types";

function renderApp(overrides = {}) {
  let admin = false;
  const api = fakeApi({
    sessions: vi.fn(async () => []),
    marketOverview: vi.fn(() => new Promise<MarketOverview>(() => undefined)),
    listApprovals: vi.fn(async () => []),
    isAdmin: vi.fn(() => admin),
    adminLogin: vi.fn(async (password: string) => {
      if (password !== "secret") throw new ApiError(401, "wrong password");
      admin = true;
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
  it("shows traders only the trader pages", () => {
    renderApp();
    const nav = screen.getByRole("navigation", { name: /pages/i });
    expect(nav).toHaveTextContent(/Chat/);
    expect(nav).toHaveTextContent(/Positions/);
    expect(nav).toHaveTextContent(/My limits/);
    for (const hidden of [/Approvals/, /Audit log/, /Test results/]) {
      expect(nav).not.toHaveTextContent(hidden);
    }
    expect(screen.queryByText(/demo market/i)).not.toBeInTheDocument();
  });

  it("opens the admin pages after the owner logs in, and closes them on log out", async () => {
    const api = renderApp();
    await userEvent.click(screen.getByRole("button", { name: /^admin$/i }));
    await userEvent.type(screen.getByLabelText(/password/i), "wrong");
    await userEvent.click(screen.getByRole("button", { name: /log in/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/wrong password/);
    await userEvent.clear(screen.getByLabelText(/password/i));
    await userEvent.type(screen.getByLabelText(/password/i), "secret");
    await userEvent.click(screen.getByRole("button", { name: /log in/i }));
    const nav = screen.getByRole("navigation", { name: /pages/i });
    expect(await screen.findByText("Audit log")).toBeInTheDocument();
    expect(nav).toHaveTextContent(/Test results/);
    await userEvent.click(screen.getByRole("button", { name: /log out/i }));
    expect(api.adminLogout).toHaveBeenCalled();
    expect(nav).not.toHaveTextContent(/Audit log/);
  });
});
