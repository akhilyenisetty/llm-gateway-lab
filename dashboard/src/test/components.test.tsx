import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { Meter } from "../components";

it.each([
  [0.5, "", "50%"],
  [0.81, "warning", "⚠ 81%"],
  [1.39, "critical", "⛔ 139%"],
])("meter at %s uses severity '%s' and always shows the number", (fraction, severity, text) => {
  const { container } = render(<Meter fraction={fraction} />);
  const fill = container.querySelector(".meter-fill")!;
  expect(fill.className.trim()).toBe(`meter-fill ${severity}`.trim());
  expect(screen.getByText(text)).toBeInTheDocument();
  // The fill never overflows its track, even over budget.
  expect((fill as HTMLElement).style.width).toBe(`${Math.min(fraction, 1) * 100}%`);
  expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", String(Math.round(fraction * 100)));
});
