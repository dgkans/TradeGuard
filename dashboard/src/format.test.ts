import { describe, expect, it } from "vitest";
import { percent, statusText } from "./format";

describe("percent", () => {
  it("rounds exactly like the Python eval reports (half to even), so numbers match RESULTS.md", () => {
    expect(percent(0.9625)).toBe("96.2%"); // Python: f"{96.25:.1f}" == "96.2"
    expect(percent(0.857)).toBe("85.7%");
    expect(percent(0.0705)).toBe("7.0%"); // 7.05 is not exact in binary; Python also gives 7.0
    expect(percent(1)).toBe("100.0%");
    expect(percent(0)).toBe("0.0%");
    expect(percent(0.2012)).toBe("20.1%");
  });
});

describe("statusText", () => {
  const base = { live: true, source: "Yahoo Finance", now: "2026-10-18T16:00:00Z",
                 market_open: false, session_start: "2026-10-19T13:30:00Z",
                 session_end: "2026-10-19T20:00:00Z", prices_as_of: "2026-10-16T20:00:00Z",
                 updated_at: "2026-10-18T15:59:30Z", stale: false };

  it("says when a closed market opens next", () => {
    expect(statusText(base)).toBe("Live · Market closed · opens Mon 9:30 AM ET");
  });

  it("says the market is open, with the price time", () => {
    expect(statusText({ ...base, market_open: true, prices_as_of: "2026-10-19T14:05:00Z" }))
      .toMatch(/^Live · Market open · .*10:05.* ET$/);
  });

  it("warns when prices are stale, and names a replayed market", () => {
    expect(statusText({ ...base, stale: true })).toBe("Live prices unavailable · orders paused");
    expect(statusText({ ...base, live: false })).toBe("Replayed market");
  });
});
