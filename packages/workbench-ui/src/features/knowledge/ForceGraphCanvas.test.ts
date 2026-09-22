import { expect, it } from "vitest";
import { findShortestPath } from "./ForceGraphCanvas";
import type { KnowledgeLink } from "./knowledgeData";
const link = (source: string, target: string): KnowledgeLink => ({ source, target, value: 1 });
it("returns no path for disconnected assets instead of fabricating a start/end pair", () => {
  expect(findShortestPath([link("a", "b"), link("c", "d")], "a", "d")).toEqual([]);
  expect(findShortestPath([], "a", "b")).toEqual([]);
});
it("does not fabricate a path to an unknown node or empty endpoint", () => {
  expect(findShortestPath([link("a", "b")], "a", "missing")).toEqual([]);
  expect(findShortestPath([], "", "")).toEqual([]);
  expect(findShortestPath([], "", "a")).toEqual([]);
});
it("keeps same-node references as a single node", () => {
  expect(findShortestPath([], "a", "a")).toEqual(["a"]);
});
it("finds a shortest association in either direction and terminates on cycles", () => {
  const links = [link("a", "b"), link("b", "c"), link("c", "a"), link("c", "d")];
  expect(findShortestPath(links, "a", "d")).toEqual(["a", "c", "d"]);
  expect(findShortestPath(links, "d", "a")).toEqual(["d", "c", "a"]);
});
