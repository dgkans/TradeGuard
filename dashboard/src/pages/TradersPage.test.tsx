import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiContext } from "../api";
import { fakeApi } from "../test/fakeApi";
import { TradersPage } from "./TradersPage";

describe("TradersPage", () => {
  it("lists every account and who is online", async () => {
    const api = fakeApi({
      traders: vi.fn(async () => [
        { id: 1, username: "asha", display_name: "Asha", profile: "default",
          starting_cash: 10000, active: true, waiting_for_approval: false, orders: 3 },
        { id: 2, username: "ravi", display_name: "Ravi", profile: "strict",
          starting_cash: 250000, active: false, waiting_for_approval: false, orders: 0 },
      ]),
    });
    render(
      <ApiContext.Provider value={api}>
        <TradersPage pollMs={0} />
      </ApiContext.Provider>,
    );
    const asha = (await screen.findByRole("rowheader", { name: /asha/i })).closest("tr");
    const ravi = screen.getByRole("rowheader", { name: /ravi/i }).closest("tr");
    if (!asha || !ravi) throw new Error("trader rows missing");
    expect(within(asha).getByText("Online")).toBeInTheDocument();
    expect(within(ravi).getByText("Strict")).toBeInTheDocument();
    expect(within(ravi).getByText("Offline")).toBeInTheDocument();
  });
});
