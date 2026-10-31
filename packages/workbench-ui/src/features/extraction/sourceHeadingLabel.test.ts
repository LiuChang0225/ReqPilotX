import { expect, it } from "vitest";
import { sourceHeadingLabel } from "./sourceHeadingLabel";

it("composes source headings without duplicating a number prefix or leaving an orphan separator", () => {
  expect(sourceHeadingLabel("1", "Scope")).toBe("1 Scope");
  expect(sourceHeadingLabel("1.", "Scope")).toBe("1. Scope");
  expect(sourceHeadingLabel("1", ". Scope")).toBe("1. Scope");
  expect(sourceHeadingLabel("1.", ". Scope")).toBe("1. Scope");
  expect(sourceHeadingLabel(".", "Scope")).toBe("Scope");
  expect(sourceHeadingLabel("3.1", "3.1 Power supply")).toBe("3.1 Power supply");
  expect(sourceHeadingLabel("3.1", "3.10 Power supply")).toBe("3.1 3.10 Power supply");
  expect(sourceHeadingLabel("3.1", "3.1.1 Power supply")).toBe("3.1 3.1.1 Power supply");
});
