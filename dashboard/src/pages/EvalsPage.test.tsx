import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiContext } from "../api";
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
  };
}

const report: EvalReport = {
  header: { date: "2026-10-17 21:32 UTC", commit: "4435fea", agent_model: "ollama:qwen3:8b",
            judge_model: "ollama:qwen3:8b", temperature: 0, repeats: 3, smoke: false },
  tuning: { no_guard: metrics(0, 0.6), rules_only: metrics(0.857, 0.9),
            full_guard: metrics(1, 0.962, true) },
  held_out: { no_guard: metrics(0, 0.6), full_guard: metrics(1, 1) },
  by_category: { no_guard: { benign: 0.93, injection: 0.88 },
                 full_guard: { benign: 0.93, injection: 0.96 } },
  judge: null,
  detector: null,
};

describe("EvalsPage", () => {
  it("shows the run header and the three config comparison with mean and spread", async () => {
    const api = fakeApi({ latestEval: vi.fn(async () => ({ name: "full_4435fea", report })) });
    render(
      <ApiContext.Provider value={api}>
        <EvalsPage />
      </ApiContext.Provider>,
    );
    expect(await screen.findByText("4435fea")).toBeInTheDocument();
    expect(screen.getAllByText(/ollama:qwen3:8b/).length).toBeGreaterThan(0);

    const tuning = screen.getByRole("table", { name: /tuning set/i });
    const header = within(tuning).getAllByRole("columnheader").map((cell) => cell.textContent);
    expect(header).toEqual(["Metric", "no_guard", "rules_only", "full_guard"]);
    const blockRow = within(tuning).getByRole("row", { name: /unsafe action block rate/i });
    expect(within(blockRow).getByText("85.7%")).toBeInTheDocument();
    const successRow = within(tuning).getByRole("row", { name: /task success rate/i });
    expect(within(successRow).getByText(/96\.2%/)).toBeInTheDocument();
    expect(within(successRow).getByText(/86\.2%.*101\.2%|\[86\.2%, 101\.2%\]/)).toBeInTheDocument();
    const p50 = within(tuning).getByRole("row", { name: /p50 gateway overhead/i });
    expect(within(p50).getAllByText("n/a").length).toBe(3);

    expect(screen.getByRole("table", { name: /held out set/i })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /unsafe action block rate by config/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /RESULTS\.md/ })).toBeInTheDocument();
  });

  it("explains when no full eval has been run", async () => {
    const { ApiError } = await import("../api");
    const api = fakeApi({
      latestEval: vi.fn(() => Promise.reject(new ApiError(404, "no full suite eval report yet"))),
    });
    render(
      <ApiContext.Provider value={api}>
        <EvalsPage />
      </ApiContext.Provider>,
    );
    expect(await screen.findByText(/make eval-full/)).toBeInTheDocument();
  });
});
