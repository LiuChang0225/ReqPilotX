import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { KnowledgeInspector } from "./KnowledgeInspector";
import type { ComplianceCase, KnowledgeNode } from "./knowledgeData";

vi.mock("./ForceGraphCanvas", async original => ({
  ...await original<object>(), ForceGraphCanvas: () => <div data-testid="local-graph" />,
}));
afterEach(cleanup);
const nodes: KnowledgeNode[] = ["source", "test"].map(id => ({ id, name: id, category: "specs", tags: [], path: `${id}.md`, val: 1, color: "" }));
function reference(id: string, verdict: ComplianceCase["verdict"]): ComplianceCase {
  return { id, title: `案例 ${id}`, verdict, matchedNodeId: "source", mappedDvpNodeId: "test",
    customerText: `案例 ${id} 的完整客户条款，包括条件、对象和验收要求。`, baselineValue: `基线 ${id}`,
    customerValue: `要求 ${id}`, reason: `判定说明 ${id}`, mappedDvpTest: `完整测试名称 ${id}` };
}
it.each([
  ["COMPLIANT", "符合"], ["DEVIATION", "有偏离"], ["CLARIFY", "待澄清"],
] as const)("shows %s as a reference verdict with the correct meaning", (status, label) => {
  render(<KnowledgeInspector currentNodeId="source" nodes={nodes} links={[{ source: "source", target: "test", value: 1 }]}
    complianceCases={[reference("A", status)]} onNavigateToNode={vi.fn()} onTracePath={vi.fn()} />);
  const cases = screen.getByRole("region", { name: "参考案例判定" });
  expect(within(cases).getByText(label)).toBeVisible();
  expect(within(cases).getByText(/不代表当前项目的评审结论/)).toBeVisible();
  expect(screen.queryByText(/完全合规|客户需求合规判定|追踪因果链/)).not.toBeInTheDocument();
});
it("preserves all matching cases and their full source text, explanation, baseline, and test", () => {
  const first = reference("A", "COMPLIANT"), second = reference("B", "CLARIFY");
  second.customerText = "带完整限定条件的原始客户条款。".repeat(12);
  render(<KnowledgeInspector currentNodeId="source" nodes={nodes} links={[]}
    complianceCases={[first, second]} onNavigateToNode={vi.fn()} onTracePath={vi.fn()} />);
  expect(screen.getByText("案例 A")).toBeVisible();
  expect(screen.getByText("案例 B")).toBeVisible();
  fireEvent.click(screen.getByText("案例 B"));
  for (const text of [second.customerText, second.reason, second.baselineValue, second.customerValue, second.mappedDvpTest]) {
    expect(screen.getByText(text)).toBeVisible();
  }
});
it("does not navigate or invent a path when the case has no known link", () => {
  const onTracePath = vi.fn();
  render(<KnowledgeInspector currentNodeId="source" nodes={nodes} links={[]}
    complianceCases={[reference("A", "DEVIATION")]} onNavigateToNode={vi.fn()} onTracePath={onTracePath} />);
  expect(screen.getByText(/暂无已知词条关联。/)).toBeVisible();
  const action = screen.getByRole("button", { name: "查看词条关联：案例 A" });
  expect(action).toBeDisabled();
  fireEvent.click(action);
  expect(onTracePath).not.toHaveBeenCalled();
});
it("only opens an association backed by the currently available nodes and links", () => {
  const onTracePath = vi.fn();
  const view = render(<KnowledgeInspector currentNodeId="source" nodes={nodes}
    links={[{ source: "source", target: "missing", value: 1 }, { source: "missing", target: "test", value: 1 }]}
    complianceCases={[reference("A", "CLARIFY")]} onNavigateToNode={vi.fn()} onTracePath={onTracePath} />);
  expect(screen.getByRole("button", { name: "查看词条关联：案例 A" })).toBeDisabled();
  view.rerender(<KnowledgeInspector currentNodeId="source" nodes={nodes}
    links={[{ source: "test", target: "source", value: 1 }]}
    complianceCases={[reference("A", "CLARIFY")]} onNavigateToNode={vi.fn()} onTracePath={onTracePath} />);
  fireEvent.click(screen.getByRole("button", { name: "查看词条关联：案例 A" }));
  expect(onTracePath).toHaveBeenCalledWith(["source", "test"]);
  expect(screen.queryByText(/暂无已知词条关联。/)).not.toBeInTheDocument();
});
it("does not retain a case from the previous asset space", () => {
  const props = { currentNodeId: "source", nodes, links: [], onNavigateToNode: vi.fn(), onTracePath: vi.fn() };
  const view = render(<KnowledgeInspector {...props} complianceCases={[reference("A", "COMPLIANT")]} />);
  view.rerender(<KnowledgeInspector {...props} complianceCases={[]} />);
  expect(screen.queryByRole("region", { name: "参考案例判定" })).not.toBeInTheDocument();
});
