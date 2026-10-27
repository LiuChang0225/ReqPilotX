import { expect, it, vi } from "vitest";
import { createApplicationNavigation } from "./ReqPilotXApplication";

it("preserves the parent location while navigating between complete workbench views", () => {
  const parent = window.location.href, saved = vi.fn(), changed = vi.fn();
  const navigation = createApplicationNavigation("/", saved);
  const dispose = navigation.subscribe(changed);
  navigation.navigate("/workspace?task=projects&projectId=p");
  expect(navigation.snapshot()).toBe("/workspace?task=projects&projectId=p");
  expect(window.location.href).toBe(parent);
  expect(saved).toHaveBeenCalledWith("/workspace?task=projects&projectId=p");
  navigation.navigate("/workspace?task=projects&projectId=p");
  expect(changed).toHaveBeenCalledOnce();
  dispose(); navigation.navigate("/home");
  expect(changed).toHaveBeenCalledOnce();
});

it("refuses links outside the embedded application", () => {
  const navigation = createApplicationNavigation();
  expect(() => navigation.navigate("https://example.com/workspace")).toThrow("不属于当前");
  expect(() => navigation.navigate("/another-product")).toThrow("不属于当前");
  expect(navigation.snapshot()).toBe("/");
});
