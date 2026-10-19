import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiContext, ApiError } from "../api";
import { fakeApi } from "../test/fakeApi";
import type { ConfigMetrics, EvalReport } from "../types";
import { EvalsPage } from "./EvalsPage";

function metrics(block: number, success: number, spread = false): ConfigMetrics {
  return {
    unsafe_action_block_rate: { mean: block, min: block, max: block, repeats: 3 },
    false_block_rate: { mean: 0, min: 0, max: 0, repeats: 3 },
    task_success_rate: spread
      ? { mean: success, min: success - 0.1, max: success + 0.05, repeats: 3 }
      : { mean: success, min: success, max: success, repeats: 3 },
    injection_success_rate: { mean: 0.05, min: 0.05, max: 0.05, repeats: 3 },
    escalation_rate: { mean: 0.2, min: 0.2, max: 0.2, repeats: 3 },
    gateway_overhead_p50_ms: { mean: null, min: null, max: null, repeats: 3 },
    gateway_overhead_p95_ms: { mean: 5.4, min: 5.0, max: 6.2, repeats: 3 },
    agent_tokens_per_scenario: { mean: 5789, min: 5717, max: 5932, repeats: 3 },
    judge_tokens_per_scenario: { mean: 37.5, min: 37.5, max: 37.5, repeats: 3 },
    scenarios: { mean: 80, min: 80, max: 80, repeats: 3 },
  };
}

const llm: EvalReport = {
  header: { date: "2026-10-17 21:32 UTC", commit: "4435fea", agent_model: "ollama:qwen3:8b",
            judge_model: "ollama:qwen3:8b", temperature: 0, repeats: 3, smoke: false },
  tuning: { no_guard: metrics(0, 0.6), rules_only: metrics(0.857, 0.9),
            full_guard: metrics(1, 0.962, true) },
  held_out: { no_guard: metrics(0, 0.6), full_guard: metrics(1, 1) },
  by_category: { no_guard: { benign: 0.93 }, full_guard: { benign: 0.93 } },
  judge: null,
  detector: null,
};

const worst: EvalReport = {
  ...llm,
  header: { ...llm.header, agent_model: "scripted:worst_case_attacker", agent: "attacker" },
  tuning: { no_guard: metrics(0, 0), rules_only: metrics(0.2, 0.2), full_guard: metrics(1, 1) },
};

function renderWith(latestEval: (agent?: "attacker") => Promise<{ name: string; report: EvalReport }>) {
  render(
    <ApiContext.Provider value={fakeApi({ latestEval: vi.fn(latestEval) })}>
      <EvalsPage />
    </ApiContext.Provider>,
  );
}

describe("EvalsPage", () => {
  it("leads with plain results for the three setups and explains each one", async () => {
    renderWith(async (agent) => (agent ? { name: "attacker_x", report: worst }
                                       : { name: "full_4435fea", report: llm }));
    const stopped = await screen.findByRole("region", { name: /unsafe orders stopped/i });
    expect(within(stopped).getByText("100.0%")).toBeInTheDocument();
    expect(within(stopped).getByText("85.7%")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /good orders wrongly blocked/i })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /requests handled correctly/i })).toBeInTheDocument();
    expect(screen.getByText(/talks to the broker directly/i)).toBeInTheDocument();
    expect(screen.getByText(/size, stock and market hours limits only/i)).toBeInTheDocument();
    expect(screen.getAllByText(/qwen3:8b/).length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: /RESULTS\.md/ })).toBeInTheDocument();
    expect(screen.queryByText(/unsafe_action_block_rate/)).not.toBeInTheDocument();
  });

  it("shows the worst case separately, and the full tables only on request", async () => {
    renderWith(async (agent) => (agent ? { name: "attacker_x", report: worst }
                                       : { name: "full_4435fea", report: llm }));
    const worstCase = await screen.findByRole("region", { name: /if the agent is fully fooled/i });
    expect(within(worstCase).getByText("100.0%")).toBeInTheDocument();
    expect(within(worstCase).getByText("20.0%")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: /tuning set/i })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /show detailed results/i }));
    const table = screen.getByRole("table", { name: /tuning set/i });
    const row = within(table).getByRole("row", { name: /task success rate/i });
    expect(within(row).getByText(/\[86\.2%, 101\.2%\]/)).toBeInTheDocument();
  });

  it("still works when there is no worst case run", async () => {
    renderWith(async (agent) => {
      if (agent) throw new ApiError(404, "none");
      return { name: "full_4435fea", report: llm };
    });
    expect(await screen.findByRole("region", { name: /unsafe orders stopped/i })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /if the agent is fully fooled/i })).not.toBeInTheDocument();
  });

  it("explains when no full eval has been run", async () => {
    renderWith(async () => {
      throw new ApiError(404, "no full suite eval report yet");
    });
    expect(await screen.findByText(/make eval-full/)).toBeInTheDocument();
  });
});
