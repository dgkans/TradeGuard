import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiContext, ApiError, type Api } from "../api";
import { approval, fakeApi } from "../test/fakeApi";
import type { Approval } from "../types";
import { ApprovalsPage } from "./ApprovalsPage";

function renderWith(api: Api) {
  return render(
    <ApiContext.Provider value={api}>
      <ApprovalsPage pollMs={0} />
    </ApiContext.Provider>,
  );
}

/** A tiny in-memory approval store behind the fake API. */
function storeApi(initial: Approval[]) {
  let items = [...initial];
  const decide = (status: "executed" | "rejected") =>
    vi.fn(async (id: string, decidedBy: string, note: string) => {
      const found = items.find((item) => item.id === id);
      if (!found) throw new ApiError(404, `no approval ${id}`);
      const updated: Approval = { ...found, status, decided_by: decidedBy, note,
                                  decided_at: "2026-10-18T15:01:00+00:00" };
      items = items.map((item) => (item.id === id ? updated : item));
      return updated;
    });
  const approve = decide("executed");
  const reject = decide("rejected");
  const listApprovals = vi.fn(async (status?: string) =>
    status ? items.filter((item) => item.status === status) : items,
  );
  return { api: fakeApi({ listApprovals, approve, reject }), approve, reject, listApprovals };
}

describe("ApprovalsPage", () => {
  it("lists pending approvals with the order, the rule and why it escalated", async () => {
    const { api } = storeApi([approval()]);
    renderWith(api);
    const card = await screen.findByRole("article", { name: /place_order NVDA/i });
    expect(within(card).getByText("approval_notional")).toBeInTheDocument();
    expect(within(card).getByText(/over the \$1,000 approval threshold/)).toBeInTheDocument();
    expect(within(card).getByText("buy $1,500 of NVDA")).toBeInTheDocument();
  });

  it("approves with the reviewer name and note, then moves it to recent decisions", async () => {
    const { api, approve } = storeApi([approval()]);
    renderWith(api);
    const card = await screen.findByRole("article", { name: /place_order NVDA/i });
    await userEvent.clear(screen.getByLabelText(/your name/i));
    await userEvent.type(screen.getByLabelText(/your name/i), "alice");
    await userEvent.type(within(card).getByLabelText(/note/i), "looks fine");
    await userEvent.click(within(card).getByRole("button", { name: /approve/i }));

    expect(approve).toHaveBeenCalledWith("apr_1", "alice", "looks fine");
    await waitFor(() =>
      expect(screen.queryByRole("article", { name: /place_order NVDA/i })).not.toBeInTheDocument(),
    );
    const recent = screen.getByRole("region", { name: /recent decisions/i });
    expect(within(recent).getByText("executed")).toBeInTheDocument();
    expect(within(recent).getByText(/alice/)).toBeInTheDocument();
  });

  it("rejects with a note", async () => {
    const { api, reject, approve } = storeApi([approval()]);
    renderWith(api);
    const card = await screen.findByRole("article", { name: /place_order NVDA/i });
    await userEvent.type(within(card).getByLabelText(/note/i), "too big");
    await userEvent.click(within(card).getByRole("button", { name: /reject/i }));
    expect(reject).toHaveBeenCalledWith("apr_1", "operator", "too big");
    expect(approve).not.toHaveBeenCalled();
    const recent = await screen.findByRole("region", { name: /recent decisions/i });
    expect(await within(recent).findByText("rejected")).toBeInTheDocument();
  });

  it("shows the server's reason when the approval was already decided or expired", async () => {
    const approve = vi.fn(() =>
      Promise.reject(new ApiError(409, "apr_1 is expired, not pending")),
    );
    const listApprovals = vi.fn(async () => [approval()]);
    renderWith(fakeApi({ listApprovals, approve }));
    const card = await screen.findByRole("article", { name: /place_order NVDA/i });
    await userEvent.click(within(card).getByRole("button", { name: /approve/i }));
    expect(await within(card).findByRole("alert")).toHaveTextContent(/expired, not pending/);
    expect(listApprovals.mock.calls.length).toBeGreaterThan(1); // refreshed after the conflict
  });

  it("disables both buttons while a decision is in flight (no double submit)", async () => {
    let finish: (value: Approval) => void = () => undefined;
    const approve = vi.fn(() => new Promise<Approval>((resolve) => { finish = resolve; }));
    renderWith(fakeApi({ listApprovals: vi.fn(async () => [approval()]), approve }));
    const card = await screen.findByRole("article", { name: /place_order NVDA/i });
    await userEvent.click(within(card).getByRole("button", { name: /approve/i }));
    expect(within(card).getByRole("button", { name: /approve/i })).toBeDisabled();
    expect(within(card).getByRole("button", { name: /reject/i })).toBeDisabled();
    finish(approval({ status: "executed" }));
  });

  it("says so when nothing is waiting", async () => {
    renderWith(fakeApi({ listApprovals: vi.fn(async () => []) }));
    expect(await screen.findByText(/nothing is waiting for approval/i)).toBeInTheDocument();
  });

  it("shows an error when the API is down", async () => {
    renderWith(fakeApi({ listApprovals: vi.fn(() => Promise.reject(new TypeError("fetch failed"))) }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/fetch failed/);
  });
});
