import { describe, expect, it } from "vitest";

import { bucketLabel, ms, pct, plural, usd } from "../format";

describe("usd", () => {
  it("keeps small amounts visible", () => {
    expect(usd("0.000124")).toBe("$0.0001");
    expect(usd("0.0081")).toBe("$0.0081");
    expect(usd("0.47")).toBe("$0.470");
    expect(usd("12.5")).toBe("$12.50");
    expect(usd("1234.5")).toBe("$1,235");
  });
  it("never shows a nonzero amount as $0", () => {
    expect(usd("0.0000086")).toBe("<$0.0001");
  });
  it("handles zero and missing", () => {
    expect(usd("0")).toBe("$0");
    expect(usd(null)).toBe("—");
  });
});

it("pluralizes counts", () => {
  expect(plural(1, "failed call")).toBe("1 failed call");
  expect(plural(3, "failed call")).toBe("3 failed calls");
});

it("formats percentages and durations", () => {
  expect(pct("0.8109")).toBe("81.1%");
  expect(pct(1, 0)).toBe("100%");
  expect(ms(950)).toBe("950 ms");
  expect(ms(2572)).toBe("2.57 s");
});

it("labels day buckets in UTC, so the date doesn't shift west of UTC", () => {
  expect(bucketLabel("2026-10-01T00:00:00Z", "day")).toMatch(/Oct 1/);
});
