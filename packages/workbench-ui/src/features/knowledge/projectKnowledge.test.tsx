import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { request } from "../../shared/api/client";
import { ProjectKnowledgePanel } from "./ProjectKnowledgePanel";
import { projectKnowledgeFileUrl, useAnalysisKnowledgeSelection, useProjectKnowledgeCatalog, validateProjectKnowledgeCatalog, type ProjectKnowledgeCatalog } from "./projectKnowledge";

vi.mock("../../shared/api/client", () => ({ request: vi.fn() }));
afterEach(() => { cleanup(); vi.mocked(request).mockReset(); localStorage.clear(); });
const sha = "a".repeat(64);
function catalog(project = "p"): ProjectKnowledgeCatalog {
  return { schema_version: "project-knowledge-facts/v1", project_id: project, knowledge_base_version: 1,
    documents: [{ id: "doc", project_id: project, file_name: "试验记录.pdf", source_sha256: sha, eligible: true, authority: "UNKNOWN", claim_role: "UNKNOWN", applicability: "UNKNOWN" }],
    evidence_catalog: { fact: { fact_id: "fact", document_id: "doc", source_sha256: sha, text: "仅用于测试的原文", source_ref: { location: { page_number: 3 } } } }, gaps: [] };
}

describe("project knowledge source identity", () => {
  it("rejects cross-project, mixed-source, duplicate and malformed catalogs", () => {
    expect(() => validateProjectKnowledgeCatalog(catalog("other"), "p")).toThrow("当前项目不一致");
    const mixed = catalog(); mixed.evidence_catalog.fact.source_sha256 = "b".repeat(64);
    expect(() => validateProjectKnowledgeCatalog(mixed, "p")).toThrow("原件版本不一致");
    const duplicate = catalog(); duplicate.documents.push(duplicate.documents[0]);
    expect(() => validateProjectKnowledgeCatalog(duplicate, "p")).toThrow("重复身份");
    const malformed = catalog(); malformed.documents = [null as never];
    expect(() => validateProjectKnowledgeCatalog(malformed, "p")).toThrow("身份不完整");
  });
  it("does not publish a late response from the previous project", async () => {
    let resolveOld!: (value: unknown) => void;
    vi.mocked(request).mockImplementation(path => String(path).includes("/old/") ? new Promise(resolve => { resolveOld = resolve; }) : Promise.resolve(catalog("new")));
    const { result, rerender } = renderHook(({ project }) => useProjectKnowledgeCatalog(project), { initialProps: { project: "old" } });
    rerender({ project: "new" });
    await waitFor(() => expect(result.current.catalog?.project_id).toBe("new"));
    await act(async () => resolveOld(catalog("old")));
    expect(result.current.catalog?.project_id).toBe("new");
  });
  it("shows registered metadata without converting readability into compliance", async () => {
    vi.mocked(request).mockResolvedValue(catalog());
    render(<ProjectKnowledgePanel projectId="p" />);
    await screen.findByRole("heading", { name: "试验记录.pdf" });
    expect(screen.getByText("未标记 · 未标记 · 适用性：未标记")).toBeVisible();
    expect(screen.getByRole("link", { name: "打开登记原件" })).toHaveAttribute("href", projectKnowledgeFileUrl("p", "doc", sha));
    expect(screen.queryByText("符合建议")).not.toBeInTheDocument();
  });
  it("does not show another file for an explicitly missing document", async () => {
    vi.mocked(request).mockResolvedValue(catalog());
    render(<ProjectKnowledgePanel projectId="p" focusedDocumentId="missing" />);
    await screen.findByText(/指定资料不在当前项目目录/);
    expect(screen.queryByRole("link", { name: "打开登记原件" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /试验记录.pdf/ })).toHaveAttribute("aria-pressed", "false");
  });
  it("keeps known catalog visible when a refresh fails", async () => {
    vi.mocked(request).mockResolvedValueOnce(catalog()).mockRejectedValueOnce(new Error("连接中断，请重试"));
    render(<ProjectKnowledgePanel projectId="p" />);
    await screen.findByRole("heading", { name: "试验记录.pdf" });
    fireEvent.click(screen.getByRole("button", { name: "同步资料" }));
    await screen.findByText("连接中断，请重试");
    expect(screen.getByRole("link", { name: "打开登记原件" })).toBeVisible();
  });
});

it("keeps default, empty and selected evidence scopes distinct across baselines", () => {
  const { result, rerender } = renderHook(({ scope }) => useAnalysisKnowledgeSelection(scope), { initialProps: { scope: "p:d:baseline-a" } });
  expect(result.current.ids).toBeNull();
  act(() => result.current.setIds([]));
  expect(result.current.ids).toEqual([]);
  rerender({ scope: "p:d:baseline-b" });
  expect(result.current.ids).toBeNull();
  act(() => result.current.setIds(["doc", "doc"]));
  expect(result.current.ids).toEqual(["doc"]);
  rerender({ scope: "p:d:baseline-a" });
  expect(result.current.ids).toEqual([]);
  act(() => result.current.setIds(null));
  expect(result.current.ids).toBeNull();
});

it("does not construct a historical evidence link without exact source identity", () => {
  expect(projectKnowledgeFileUrl("p", "doc", "")).toBe("");
  expect(projectKnowledgeFileUrl("", "doc", sha)).toBe("");
  expect(projectKnowledgeFileUrl("p", "doc", sha)).toContain(`expected_source_sha256=${sha}`);
});
