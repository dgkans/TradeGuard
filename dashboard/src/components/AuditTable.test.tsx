import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { auditEvent } from "../test/fakeApi";
import { AuditTable } from "./AuditTable";

describe("AuditTable", () => {
  it("renders one row per event with decision, rule, latency and a trace link", () => {
    render(
      <AuditTable
        events={[
          auditEvent(),
          auditEvent({ id: 2, tool: "get_quote", decision: "ALLOW", rule_id: "pass",
                       args: { symbol: "AMD" }, overhead_ms: 0.25, trace_url: null }),
        ]}
      />,
    );
    const rows = screen.getAllByRole("row").slice(1); // skip the header row
    expect(rows).toHaveLength(2);

    const denied = within(rows[0] as HTMLElement);
    expect(denied.getByText("place_order")).toBeInTheDocument();
    expect(denied.getByText("DENY")).toHaveClass("badge-deny");
    expect(denied.getByText("max_order_notional")).toBeInTheDocument();
    expect(denied.getByText(/order notional \$3,000/)).toBeInTheDocument();
    expect(denied.getByText(/"notional":3000/)).toBeInTheDocument();
    expect(denied.getByText("0.50 ms")).toBeInTheDocument();
    expect(denied.getByRole("link", { name: /trace/i })).toHaveAttribute(
      "href",
      `http://localhost:16686/trace/${"ab".repeat(16)}`,
    );

    const allowed = within(rows[1] as HTMLElement);
    expect(allowed.getByText("ALLOW")).toHaveClass("badge-allow");
    expect(allowed.getByText("0.25 ms")).toBeInTheDocument();
    expect(allowed.queryByRole("link", { name: /trace/i })).not.toBeInTheDocument();
  });

  it("shows an empty state instead of an empty table", () => {
    render(<AuditTable events={[]} />);
    expect(screen.getByText(/no audit events/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("lets the user filter by a row's session", async () => {
    const onSession = vi.fn();
    render(<AuditTable events={[auditEvent()]} onSession={onSession} />);
    await userEvent.click(screen.getByRole("button", { name: "web-demo-1" }));
    expect(onSession).toHaveBeenCalledWith("web-demo-1");
  });

  it("shows a dash when the call has not finished (no latency yet)", () => {
    render(<AuditTable events={[auditEvent({ overhead_ms: null, latency_ms: null })]} />);
    const row = screen.getAllByRole("row")[1] as HTMLElement;
    expect(within(row).getByText("–")).toBeInTheDocument();
  });
});
