import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiContext, ApiError } from "../api";
import { fakeApi } from "../test/fakeApi";
import type { SessionInfo } from "../types";
import { TraderBar } from "./TraderBar";

function info(user: string, overrides: Partial<SessionInfo> = {}): SessionInfo {
  return { session_id: `web-${user.toLowerCase()}-1`, user, scenario: "desk", guarded: true,
           policy: "default", model: "ollama:qwen3:8b", started_at: "2026-10-18T15:00:00Z",
           busy: false, waiting_for_approval: false, ...overrides };
}

describe("TraderBar", () => {
  it("starts a session for a named trader with a risk profile", async () => {
    const createSession = vi.fn(async () => info("Asha"));
    const onStarted = vi.fn();
    render(
      <ApiContext.Provider value={fakeApi({ createSession })}>
        <TraderBar sessions={[]} activeId={null} onSelect={vi.fn()} onStarted={onStarted}
                   onEnded={vi.fn()} />
      </ApiContext.Provider>,
    );
    await userEvent.type(screen.getByLabelText(/trader name/i), "Asha");
    await userEvent.selectOptions(screen.getByLabelText(/risk profile/i), "strict");
    await userEvent.click(screen.getByRole("button", { name: /start session/i }));
    expect(createSession).toHaveBeenCalledWith("Asha", "strict");
    expect(onStarted).toHaveBeenCalledWith(info("Asha"));
  });

  it("lists every trader with their status and an End button each", async () => {
    const closeSession = vi.fn(async () => undefined);
    const onEnded = vi.fn();
    const onSelect = vi.fn();
    const sessions = [info("Asha", { waiting_for_approval: true }), info("Ben", { busy: true })];
    render(
      <ApiContext.Provider value={fakeApi({ closeSession })}>
        <TraderBar sessions={sessions} activeId="web-asha-1" onSelect={onSelect}
                   onStarted={vi.fn()} onEnded={onEnded} />
      </ApiContext.Provider>,
    );
    const list = screen.getByRole("list", { name: /traders/i });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0] as HTMLElement).getByText(/waiting for approval/i)).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText(/working/i)).toBeInTheDocument();
    expect(within(rows[0] as HTMLElement).getByRole("button", { name: "Asha" }))
      .toHaveAttribute("aria-pressed", "true");

    await userEvent.click(within(rows[1] as HTMLElement).getByRole("button", { name: "Ben" }));
    expect(onSelect).toHaveBeenCalledWith("web-ben-1");
    await userEvent.click(screen.getByRole("button", { name: /end ben's session/i }));
    expect(closeSession).toHaveBeenCalledWith("web-ben-1");
    expect(onEnded).toHaveBeenCalledWith("web-ben-1");
  });

  it("shows why a session could not start", async () => {
    const createSession = vi.fn(() =>
      Promise.reject(new ApiError(409, "Asha already has an open session; end it first")),
    );
    render(
      <ApiContext.Provider value={fakeApi({ createSession })}>
        <TraderBar sessions={[]} activeId={null} onSelect={vi.fn()} onStarted={vi.fn()}
                   onEnded={vi.fn()} />
      </ApiContext.Provider>,
    );
    await userEvent.type(screen.getByLabelText(/trader name/i), "Asha");
    await userEvent.click(screen.getByRole("button", { name: /start session/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/already has an open session/);
  });

  it("folds the new session form away once someone is trading", async () => {
    render(
      <ApiContext.Provider value={fakeApi()}>
        <TraderBar sessions={[info("Asha")]} activeId="web-asha-1" onSelect={vi.fn()}
                   onStarted={vi.fn()} onEnded={vi.fn()} />
      </ApiContext.Provider>,
    );
    expect(screen.queryByLabelText(/trader name/i)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /new session/i }));
    expect(screen.getByLabelText(/trader name/i)).toBeInTheDocument();
  });
});

