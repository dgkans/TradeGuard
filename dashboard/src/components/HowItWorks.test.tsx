import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { HowItWorks } from "./HowItWorks";

describe("HowItWorks", () => {
  it("explains the flow with a worked example and closes", async () => {
    const onClose = vi.fn();
    render(<HowItWorks open onClose={onClose} />);
    const dialog = screen.getByRole("dialog", { name: /how tradeguard works/i });
    const flow = within(dialog).getByRole("list", { name: /flow/i });
    expect(within(flow).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      expect.stringMatching(/^You/),
      expect.stringMatching(/^AI agent/),
      expect.stringMatching(/^TradeGuard/),
      expect.stringMatching(/^Broker/),
    ]);
    const example = within(dialog).getByRole("list", { name: /example/i });
    expect(within(example).getByText("Buy $1500 of NVDA")).toBeInTheDocument();
    expect(within(example).getByText(/holds the order and asks you/)).toBeInTheDocument();
    expect(within(dialog).getByText(/plug/i)).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: /close/i }));
    expect(onClose).toHaveBeenCalled();
  });

  it("renders nothing when closed", () => {
    render(<HowItWorks open={false} onClose={vi.fn()} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
