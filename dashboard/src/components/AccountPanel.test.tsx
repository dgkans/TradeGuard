import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiContext, ApiError } from "../api";
import { fakeApi } from "../test/fakeApi";
import type { Me } from "../types";
import { AccountPanel } from "./AccountPanel";

function setup(me: Me | null, overrides = {}) {
  const api = fakeApi(overrides);
  const onAuthed = vi.fn();
  const onLoggedOut = vi.fn();
  render(
    <ApiContext.Provider value={api}>
      <AccountPanel me={me} onAuthed={onAuthed} onLoggedOut={onLoggedOut} />
    </ApiContext.Provider>,
  );
  return { api, onAuthed, onLoggedOut };
}

describe("AccountPanel", () => {
  it("signs up with a starting cash amount and a profile", async () => {
    const { api, onAuthed } = setup(null, { signup: vi.fn(async () => undefined) });
    await userEvent.click(screen.getByRole("tab", { name: /sign up/i }));
    await userEvent.type(screen.getByLabelText(/username/i), "ravi");
    await userEvent.type(screen.getByLabelText(/^password/i), "a-long-password");
    await userEvent.selectOptions(screen.getByLabelText(/risk profile/i), "strict");
    const cash = screen.getByLabelText(/starting cash/i);
    await userEvent.clear(cash);
    await userEvent.type(cash, "250000");
    await userEvent.click(screen.getByRole("button", { name: /create account/i }));
    expect(api.signup).toHaveBeenCalledWith({
      username: "ravi", password: "a-long-password", starting_cash: 250000, profile: "strict",
      display_name: undefined,
    });
    expect(onAuthed).toHaveBeenCalledWith("trader");
  });

  it("won't sign up with cash outside $1,000 to $1,000,000", async () => {
    setup(null);
    await userEvent.click(screen.getByRole("tab", { name: /sign up/i }));
    await userEvent.type(screen.getByLabelText(/username/i), "ravi");
    await userEvent.type(screen.getByLabelText(/^password/i), "a-long-password");
    const cash = screen.getByLabelText(/starting cash/i);
    await userEvent.clear(cash);
    await userEvent.type(cash, "2000000");
    expect(cash).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: /create account/i })).toBeDisabled();
  });

  it("tells the app whether a trader or the admin logged in", async () => {
    const { onAuthed } = setup(null, { login: vi.fn(async () => "admin" as const) });
    await userEvent.type(screen.getByLabelText(/username/i), "admin");
    await userEvent.type(screen.getByLabelText(/password/i), "the-owner-password");
    await userEvent.click(screen.getByRole("button", { name: /^log in$/i }));
    expect(onAuthed).toHaveBeenCalledWith("admin");
  });

  it("shows the admin card and logs the admin out", async () => {
    const api = fakeApi();
    const onLoggedOut = vi.fn();
    render(
      <ApiContext.Provider value={api}>
        <AccountPanel me={null} admin onAuthed={vi.fn()} onLoggedOut={onLoggedOut} />
      </ApiContext.Provider>,
    );
    expect(screen.getByText("Admin")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /log out/i }));
    expect(api.adminLogout).toHaveBeenCalled();
    expect(onLoggedOut).toHaveBeenCalled();
  });

  it("shows why a log in failed", async () => {
    const { onAuthed } = setup(null, {
      login: vi.fn(async () => {
        throw new ApiError(401, "wrong username or password");
      }),
    });
    await userEvent.type(screen.getByLabelText(/username/i), "ravi");
    await userEvent.type(screen.getByLabelText(/password/i), "nope");
    await userEvent.click(screen.getByRole("button", { name: /^log in$/i }));
    expect(await screen.findByText(/wrong username or password/)).toBeInTheDocument();
    expect(onAuthed).not.toHaveBeenCalled();
  });

  it("shows the signed-in account and logs out", async () => {
    const { api, onLoggedOut } = setup({
      trader: { id: 2, username: "ravi", display_name: "Ravi", profile: "strict",
                starting_cash: 250000 },
      session: { session_id: "web-2-ravi", user: "Ravi", scenario: "desk", guarded: true,
                 policy: "strict", model: "m", started_at: "2026-10-18T15:00:00Z",
                 busy: false, waiting_for_approval: true },
    });
    expect(screen.getByText("Ravi")).toBeInTheDocument();
    expect(screen.getByText("Strict")).toBeInTheDocument();
    expect(screen.getByText(/waiting for your approval/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /log out/i }));
    expect(api.logout).toHaveBeenCalled();
    expect(onLoggedOut).toHaveBeenCalled();
  });
});
