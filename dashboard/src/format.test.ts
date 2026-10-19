import { describe, expect, it } from "vitest";
import { percent } from "./format";

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
