import { useState, type ComponentProps } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../../shared/api/client";
import { WorkbenchHostContext } from "../workspace/WorkbenchHostContext";
import { NativeEvidenceReview } from "./NativeEvidenceReview";
import { projectKnowledgeFileUrl, type ProjectKnowledgeCatalog } from "./projectKnowledge";
import type { EvidenceScope, EvidenceSession, OpenKbEvidenceStatus, OriginalEvidenceResult, PreparedEvidenceReview } from "./openKbEvidenceApi";

vi.mock("../../shared/api/client", () => ({ request: vi.fn() }));
beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function(this: HTMLDialogElement) { this.setAttribute("open", ""); } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function(this: HTMLDialogElement) { this.removeAttribute("open"); } });
});
afterEach(() => { cleanup(); vi.mocked(request).mockReset(); });

const scope: EvidenceScope = { projectId: "p", documentId: "sor", sourceSha256: "a".repeat(64), baselineSha256: "b".repeat(64) };
const docSha = "e".repeat(64), secondSha = "f".repeat(64);
const generation = `generation-${"d".repeat(64)}`;
const evidenceId = (index: number) => `original-evidence-${index.toString(16).padStart(64, "0")}`;
function status(): OpenKbEvidenceStatus {
  return { available: true, knowledge_base_version: 7, catalog_sha256: "c".repeat(64), generation, live_openkb: "NOT_RUN" };
}
function catalog(project = "p"): ProjectKnowledgeCatalog {
  return {
    schema_version: "project-knowledge-facts/v1", project_id: project, knowledge_base_version: 7,
    documents: [
      { id: "kb-one", project_id: project, file_name: "试验记录.pdf", source_sha256: docSha, eligible: true },
      { id: "kb-two", project_id: project, file_name: "设计说明.pdf", source_sha256: secondSha, eligible: true },
    ], evidence_catalog: {}, gaps: [],
  };
}
function session(currentScope = scope, unitIds = ["unit-1"]): EvidenceSession {
  return {
    session_id: "1".repeat(32), project_id: currentScope.projectId, document_id: currentScope.documentId,
    source_sha256: currentScope.sourceSha256, baseline_sha256: currentScope.baselineSha256,
    generation, packet_sha256: "2".repeat(64), knowledge_base_version: 7,
    // A selected catalog has its own identity; the directory identity is used for freshness checks.
    catalog_sha256: "3".repeat(64), directory_catalog_sha256: "c".repeat(64), unit_ids: unitIds,
    catalog_gaps: [{ code: "SOURCE_AUTHORITY_UNKNOWN", message: "登记来源权威尚待核对" }],
    human_acceptance: "NOT_CLAIMED", live_openkb: "NOT_RUN",
  };
}
function originals(document = "kb-one", count = 2, project = "p"): OriginalEvidenceResult {
  const source = document === "kb-one" ? docSha : secondSha;
  return {
    generation, document_id: document, assessment: "NOT_PERFORMED",
    evidence: Array.from({ length: count }, (_, index) => {
      const id = index + (document === "kb-one" ? 1 : 101);
      const text = `${document} 实际登记原文 ${index + 1}`;
      const sourceRef = { format: "pdf", source_node_id: `node-${id}`, location: { page_number: index + 1 } };
      return {
        evidence_id: evidenceId(id), text, source_ref: sourceRef, authority: "UNKNOWN", applicability: "UNKNOWN",
        reference: {
          schema_version: "openkb-original-reference/v1", project_id: project, kb: `project-${project}`, generation,
          document_id: document, source_sha256: source, fact_id: `fact-${id}`, source_ref: sourceRef,
          quote_start: 0, quote_end: text.length, excerpt: text, text_sha256: "4".repeat(64),
        },
      };
    }), gaps: [{ code: "VISUAL_EVIDENCE_REQUIRES_READING", message: `${document} 图片需结合登记原件阅读` }],
  };
}
function prepared(): PreparedEvidenceReview {
  return { status: "HOST_PENDING_DRAFT_SAVED", job_id: null, assessment_sha256: "5".repeat(64), pending_unit_count: 1,
    human_acceptance: "NOT_CLAIMED", live_openkb: "NOT_RUN", semantic_validation: "NOT_PERFORMED" };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
type Transport = {
  catalog?: (project: string) => unknown | Promise<unknown>;
  status?: () => unknown | Promise<unknown>;
  session?: (init: RequestInit, project: string, document: string) => unknown | Promise<unknown>;
  originals?: (document: string, init: RequestInit) => unknown | Promise<unknown>;
  prepare?: (init: RequestInit) => unknown | Promise<unknown>;
};
function installTransport(overrides: Transport = {}) {
  vi.mocked(request).mockImplementation((path, init = {}) => {
    const project = decodeURIComponent(path.split("/")[3]);
    let response: unknown;
    if (path.endsWith("/knowledge-facts")) response = overrides.catalog?.(project) ?? catalog(project);
    else if (path.endsWith("/status")) response = overrides.status?.() ?? status();
    else if (path.endsWith("/prepare-review")) response = overrides.prepare?.(init) ?? prepared();
    else if (path.includes("/originals/")) {
      const document = decodeURIComponent(path.split("/").at(-1)!);
      response = overrides.originals?.(document, init) ?? originals(document, 2, project);
    } else if (path.endsWith("/sessions")) {
      const document = decodeURIComponent(path.split("/")[5]);
      const body = JSON.parse(String(init.body)) as { expected_source_sha256: string; expected_baseline_sha256: string; selected_unit_ids: string[] };
      response = overrides.session?.(init, project, document) ?? session({ projectId: project, documentId: document,
        sourceSha256: body.expected_source_sha256, baselineSha256: body.expected_baseline_sha256 }, body.selected_unit_ids);
    } else throw new Error(`Unexpected request: ${path}`);
    return Promise.resolve(response) as ReturnType<typeof request>;
  });
}
function calls(suffix: string) { return vi.mocked(request).mock.calls.filter(([path]) => path.endsWith(suffix)); }
function props(overrides: Partial<ComponentProps<typeof NativeEvidenceReview>> = {}) {
  return { scope, unitIds: ["unit-1"], unitLabel: "当前条目", knowledgeIds: ["kb-one"] as string[] | null,
    onKnowledgeChange: vi.fn(), onClose: vi.fn(), onPrepared: vi.fn(), ...overrides };
}
async function ready() {
  await waitFor(() => expect(screen.getByRole("button", { name: "读取所选资料原文" })).toBeEnabled());
  await act(async () => {});
}
async function read() {
  await ready(); fireEvent.click(screen.getByRole("button", { name: "读取所选资料原文" }));
  await screen.findByRole("region", { name: "证据原文选择" });
}

describe("native evidence source-only review", () => {
  it("reads the selected actual originals and saves only a pending draft with exact packet identity", async () => {
    installTransport(); const input = props();
    render(<NativeEvidenceReview {...input} />);
    await read();
    expect(JSON.parse(String(calls("/sessions")[0][1]?.body))).toEqual({ expected_source_sha256: scope.sourceSha256,
      expected_baseline_sha256: scope.baselineSha256, expected_knowledge_base_version: 7,
      knowledge_document_ids: ["kb-one"], selected_unit_ids: ["unit-1"] });
    expect(calls("/sessions")[0][0]).toBe("/api/projects/p/sor-documents/sor/agent-structure/delivery/openkb-evidence/sessions");
    expect(calls("/originals/kb-one")).toHaveLength(1);
    expect(calls("/originals/kb-two")).toHaveLength(0);
    expect(screen.getByText("kb-one 实际登记原文 1")).toBeVisible();
    expect(screen.getByRole("region", { name: "证据原文选择" })).toHaveTextContent("已读取来源证据包 · 1 份资料 · 2 段原文");
    const originalCheckbox = screen.getByRole("checkbox", { name: `选择原文 ${evidenceId(1)}` });
    expect(originalCheckbox).not.toBeChecked();
    fireEvent.click(originalCheckbox);
    expect(screen.getByText(/已选 1 \/ 32 段原文/)).toBeInTheDocument();
    const originalLinks = screen.getAllByRole("link", { name: "打开此证据登记原件" });
    expect(originalLinks[0]).toHaveAttribute("href", projectKnowledgeFileUrl("p", "kb-one", docSha));
    expect(screen.getByRole("complementary", { name: "证据缺口" })).toHaveTextContent("登记来源权威尚待核对");
    expect(screen.getByRole("complementary", { name: "证据缺口" })).toHaveTextContent("图片需结合登记原件阅读");
    fireEvent.click(screen.getByRole("button", { name: "准备待证据评审草稿" }));
    await screen.findByText(/已为本次选定单元保存待内部证据工作草稿（PENDING_EVIDENCE）/);
    expect(JSON.parse(String(calls("/prepare-review")[0][1]?.body))).toEqual({
      expected_packet_sha256: session().packet_sha256, evidence_ids: [evidenceId(1)],
    });
    expect(calls("/prepare-review")[0][0]).toContain(`/sessions/${session().session_id}/prepare-review`);
    expect(input.onPrepared).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "评审草稿已保存" })).toBeDisabled();
    expect(originalCheckbox).toBeDisabled();
    expect(screen.getByText(/语义比较未执行（NOT_PERFORMED）.*实时检索未运行（NOT_RUN）.*人工接受未声明（NOT_CLAIMED）/)).toBeInTheDocument();
    expect(vi.mocked(request).mock.calls.every(([path]) => !/run-ai|retriev|index|model|confirmation/.test(path))).toBe(true);
    // The selected catalog identity deliberately differs from the directory identity.
    expect(session().catalog_sha256).not.toBe(status().catalog_sha256);
  });

  it.each([null, []] as Array<string[] | null>)("preserves the distinction between default and explicit-empty document scope (%s)", async knowledgeIds => {
    installTransport(); render(<NativeEvidenceReview {...props({ knowledgeIds })} />);
    await read();
    expect(JSON.parse(String(calls("/sessions")[0][1]?.body)).knowledge_document_ids).toEqual(knowledgeIds);
    expect(calls("/originals/kb-one")).toHaveLength(knowledgeIds === null ? 1 : 0);
    expect(calls("/originals/kb-two")).toHaveLength(knowledgeIds === null ? 1 : 0);
    if (knowledgeIds?.length === 0) expect(screen.getByText(/本次未选择项目资料；草稿仅保留证据待补充状态/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "准备待证据评审草稿" }));
    await screen.findByText(/PENDING_EVIDENCE/);
    expect(JSON.parse(String(calls("/prepare-review")[0][1]?.body)).evidence_ids).toEqual([]);
  });

  it("limits original selection to 32 while allowing deselection at the limit", async () => {
    installTransport({ originals: document => originals(document, 33) });
    render(<NativeEvidenceReview {...props()} />); await read();
    const choices = screen.getAllByRole("checkbox", { name: /^选择原文 / });
    for (const checkbox of choices.slice(0, 32)) fireEvent.click(checkbox);
    expect(screen.getByText(/已选 32 \/ 32 段原文/)).toBeInTheDocument();
    expect(choices[32]).toBeDisabled();
    expect(choices[0]).toBeEnabled();
    fireEvent.click(choices[0]);
    expect(choices[32]).toBeEnabled();
    fireEvent.click(choices[32]);
    fireEvent.click(screen.getByRole("button", { name: "准备待证据评审草稿" }));
    await screen.findByText(/PENDING_EVIDENCE/);
    const selected = JSON.parse(String(calls("/prepare-review")[0][1]?.body)).evidence_ids as string[];
    expect(selected).toHaveLength(32);
    expect(selected).not.toContain(evidenceId(1));
    expect(selected).toContain(evidenceId(33));
  });

  it("prevents read and prepare duplicates from synchronous repeated clicks", async () => {
    const reading = deferred<unknown>(), saving = deferred<unknown>();
    installTransport({ originals: () => reading.promise, prepare: () => saving.promise });
    const input = props(); render(<NativeEvidenceReview {...input} />); await ready();
    const readButton = screen.getByRole("button", { name: "读取所选资料原文" });
    act(() => { fireEvent.click(readButton); fireEvent.click(readButton); fireEvent.click(readButton); });
    await waitFor(() => expect(calls("/originals/kb-one")).toHaveLength(1));
    expect(calls("/sessions")).toHaveLength(1);
    await act(async () => reading.resolve(originals()));
    const prepareButton = screen.getByRole("button", { name: "准备待证据评审草稿" });
    act(() => { fireEvent.click(prepareButton); fireEvent.click(prepareButton); fireEvent.click(prepareButton); });
    await waitFor(() => expect(calls("/prepare-review")).toHaveLength(1));
    await act(async () => saving.resolve(prepared()));
    expect(input.onPrepared).toHaveBeenCalledTimes(1);
  });

  it("retains valid originals but blocks preparation when another original request fails", async () => {
    installTransport({ originals: document => document === "kb-one" ? originals() : Promise.reject(new Error("原件读取失败")) });
    render(<NativeEvidenceReview {...props({ knowledgeIds: null })} />); await read();
    expect(screen.getByText("kb-one 实际登记原文 1")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("设计说明.pdf：原件读取失败");
    expect(screen.getByRole("alert")).toHaveTextContent("部分原文未读取，暂不能准备草稿");
    expect(screen.getByRole("button", { name: "准备待证据评审草稿" })).toBeDisabled();
    expect(calls("/prepare-review")).toHaveLength(0);
    expect(screen.getByRole("button", { name: "重新读取证据包" })).toBeEnabled();
  });

  it("shows no-text originals as a content gap, without treating them as a negative finding", async () => {
    installTransport({ originals: document => originals(document, 0) });
    render(<NativeEvidenceReview {...props()} />); await read();
    expect(screen.getByText(/此资料当前没有可引用文字/)).toHaveTextContent("不能据此判断不存在证据");
    expect(screen.queryByRole("checkbox", { name: /^选择原文 / })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "准备待证据评审草稿" })).toBeEnabled();
  });
});

describe("native evidence exact identity checks", () => {
  it.each([
    ["project", { project_id: "other" }], ["document", { document_id: "other" }],
    ["source", { source_sha256: "9".repeat(64) }], ["baseline", { baseline_sha256: "9".repeat(64) }],
    ["generation", { generation: `generation-${"9".repeat(64)}` }],
    ["directory catalog", { directory_catalog_sha256: "9".repeat(64) }],
    ["knowledge version", { knowledge_base_version: 8 }], ["selected catalog hash", { catalog_sha256: "invalid" }],
    ["packet hash", { packet_sha256: "invalid" }], ["unit scope", { unit_ids: ["other-unit"] }],
    ["session identifier", { session_id: "invalid" }], ["human acceptance", { human_acceptance: "CLAIMED" }],
  ])("rejects mismatched %s in the session before displaying originals", async (_name, changes) => {
    installTransport({ session: () => ({ ...session(), ...changes }) });
    render(<NativeEvidenceReview {...props()} />); await ready();
    fireEvent.click(screen.getByRole("button", { name: "读取所选资料原文" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("原文证据包与当前项目、文档或分析单元不一致");
    expect(calls("/originals/kb-one")).toHaveLength(0);
    expect(screen.queryByRole("region", { name: "证据原文选择" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "准备待证据评审草稿" })).not.toBeInTheDocument();
  });

  it.each([
    ["source version", (value: OriginalEvidenceResult) => { value.evidence[0].reference.source_sha256 = "9".repeat(64); }],
    ["project", (value: OriginalEvidenceResult) => { value.evidence[0].reference.project_id = "other"; }],
    ["document", (value: OriginalEvidenceResult) => { value.evidence[0].reference.document_id = "other"; }],
    ["generation", (value: OriginalEvidenceResult) => { value.evidence[0].reference.generation = `generation-${"9".repeat(64)}`; }],
    ["excerpt", (value: OriginalEvidenceResult) => { value.evidence[0].reference.excerpt = "unverified excerpt"; }],
    ["locator", (value: OriginalEvidenceResult) => { value.evidence[0].reference.source_ref = { location: { page_number: 99 } }; }],
    ["quote range", (value: OriginalEvidenceResult) => { value.evidence[0].reference.quote_end = -1; }],
    ["text hash", (value: OriginalEvidenceResult) => { value.evidence[0].reference.text_sha256 = "invalid"; }],
    ["evidence identifier", (value: OriginalEvidenceResult) => { value.evidence[0].evidence_id = "invalid"; }],
    ["duplicate evidence", (value: OriginalEvidenceResult) => { value.evidence.push(value.evidence[0]); }],
  ])("never renders unverified originals with mismatched %s", async (_name, mutate) => {
    const value = originals(); mutate(value);
    installTransport({ originals: () => value });
    render(<NativeEvidenceReview {...props()} />); await read();
    expect(screen.getByRole("alert")).toHaveTextContent("原文引用与当前资料版本不一致");
    expect(screen.queryByText("kb-one 实际登记原文 1")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "打开此证据登记原件" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "准备待证据评审草稿" })).toBeDisabled();
  });

  it.each([
    ["generation", { generation: `generation-${"9".repeat(64)}` }],
    ["document", { document_id: "other" }], ["assessment", { assessment: "PERFORMED" }],
  ])("rejects original response envelope with mismatched %s", async (_name, changes) => {
    installTransport({ originals: () => ({ ...originals(), ...changes }) });
    render(<NativeEvidenceReview {...props()} />); await read();
    expect(screen.getByRole("alert")).toHaveTextContent("原文证据响应不完整");
    expect(screen.queryByText("kb-one 实际登记原文 1")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "准备待证据评审草稿" })).toBeDisabled();
  });

  it("rejects duplicate evidence identities across different originals", async () => {
    installTransport({ originals: document => {
      const value = originals(document); value.evidence[0].evidence_id = evidenceId(1); return value;
    } });
    render(<NativeEvidenceReview {...props({ knowledgeIds: null })} />); await ready();
    fireEvent.click(screen.getByRole("button", { name: "读取所选资料原文" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("不同资料返回了重复证据身份");
    expect(screen.queryByRole("region", { name: "证据原文选择" })).not.toBeInTheDocument();
  });

  it("blocks reading when the catalog selection contains missing or ineligible documents", async () => {
    installTransport({ catalog: project => {
      const value = catalog(project); value.documents[1].eligible = false; return value;
    } });
    render(<NativeEvidenceReview {...props({ knowledgeIds: ["kb-two", "missing"] })} />);
    await screen.findByText(/部分所选资料已失效/);
    expect(screen.getByRole("button", { name: "读取所选资料原文" })).toBeDisabled();
    expect(calls("/sessions")).toHaveLength(0);
  });

  it("blocks stale catalog versions before creating an evidence session", async () => {
    installTransport({ status: () => ({ ...status(), knowledge_base_version: 8 }) });
    render(<NativeEvidenceReview {...props()} />); await ready();
    fireEvent.click(screen.getByRole("button", { name: "读取所选资料原文" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("项目资料已更新，请先同步资料目录");
    expect(calls("/sessions")).toHaveLength(0);
  });

  it.each([
    ["generation", { generation: `generation-${"9".repeat(64)}` }],
    ["directory catalog", { catalog_sha256: "9".repeat(64) }],
    ["knowledge version", { knowledge_base_version: 8 }], ["availability", { available: false }],
  ])("rechecks %s before preparing and requires a fresh packet when it changes", async (_name, changes) => {
    let statusReads = 0;
    installTransport({ status: () => ++statusReads === 1 ? status() : { ...status(), ...changes } });
    const input = props(); render(<NativeEvidenceReview {...input} />); await read();
    fireEvent.click(screen.getByRole("button", { name: "准备待证据评审草稿" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("项目资料版本已变化");
    expect(screen.getByRole("alert")).toHaveTextContent("先同步工作账本");
    expect(calls("/prepare-review")).toHaveLength(0);
    expect(input.onPrepared).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("region", { name: "证据原文选择" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "读取所选资料原文" })).toBeEnabled();
  });
});

describe("native evidence pending-request lifecycle", () => {
  const scopeChanges: Array<[string, Partial<ComponentProps<typeof NativeEvidenceReview>>]> = [
    ["baseline", { scope: { ...scope, baselineSha256: "8".repeat(64) } }],
    ["source", { scope: { ...scope, sourceSha256: "8".repeat(64) } }],
    ["document", { scope: { ...scope, documentId: "new-sor" } }],
    ["project", { scope: { ...scope, projectId: "new-project" } }],
    ["unit selection", { unitIds: ["unit-2"] }], ["knowledge selection", { knowledgeIds: ["kb-two"] }],
  ];

  it.each(scopeChanges)("aborts and ignores a late original read after changing %s", async (_name, changes) => {
    const pending = deferred<unknown>(); installTransport({ originals: () => pending.promise });
    const input = props(); const view = render(<NativeEvidenceReview {...input} />); await ready();
    fireEvent.click(screen.getByRole("button", { name: "读取所选资料原文" }));
    await waitFor(() => expect(calls("/originals/kb-one")).toHaveLength(1));
    const oldSignal = calls("/originals/kb-one")[0][1]?.signal;
    view.rerender(<NativeEvidenceReview {...input} {...changes} />);
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => pending.resolve(originals()));
    expect(screen.queryByRole("region", { name: "证据原文选择" })).not.toBeInTheDocument();
    expect(screen.queryByText("kb-one 实际登记原文 1")).not.toBeInTheDocument();
    expect(input.onPrepared).not.toHaveBeenCalled();
    await ready();
  });

  it.each(scopeChanges)("does not publish a late draft after changing %s", async (_name, changes) => {
    const pending = deferred<unknown>(); installTransport({ prepare: () => pending.promise });
    const input = props(); const view = render(<NativeEvidenceReview {...input} />); await read();
    fireEvent.click(screen.getByRole("checkbox", { name: `选择原文 ${evidenceId(1)}` }));
    fireEvent.click(screen.getByRole("button", { name: "准备待证据评审草稿" }));
    await waitFor(() => expect(calls("/prepare-review")).toHaveLength(1));
    const oldSignal = calls("/prepare-review")[0][1]?.signal;
    view.rerender(<NativeEvidenceReview {...input} {...changes} />);
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => pending.resolve(prepared()));
    expect(screen.queryByRole("button", { name: "评审草稿已保存" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "证据原文选择" })).not.toBeInTheDocument();
    expect(input.onPrepared).not.toHaveBeenCalled();
    await ready();
  });

  it("invalidates a pending read when a refreshed catalog changes version", async () => {
    let catalogReads = 0;
    const pending = deferred<unknown>();
    installTransport({ catalog: project => ({ ...catalog(project), knowledge_base_version: ++catalogReads === 1 ? 7 : 8 }), originals: () => pending.promise });
    render(<NativeEvidenceReview {...props()} />); await ready();
    fireEvent.click(screen.getByRole("button", { name: "读取所选资料原文" }));
    await waitFor(() => expect(calls("/originals/kb-one")).toHaveLength(1));
    const oldSignal = calls("/originals/kb-one")[0][1]?.signal;
    fireEvent.click(screen.getByRole("button", { name: "同步资料" }));
    await waitFor(() => expect(oldSignal?.aborted).toBe(true));
    await act(async () => pending.resolve(originals()));
    expect(screen.queryByRole("region", { name: "证据原文选择" })).not.toBeInTheDocument();
    await ready();
  });

  it.each(["read", "prepare"] as const)("closes and reopens safely while %s is pending", async phase => {
    const pending = deferred<unknown>();
    installTransport(phase === "read" ? { originals: () => pending.promise } : { prepare: () => pending.promise });
    const input = props();
    function Harness() {
      const [open, setOpen] = useState(true);
      return <>{open ? <NativeEvidenceReview {...input} onClose={() => { input.onClose(); setOpen(false); }} />
        : <button type="button" onClick={() => setOpen(true)}>重新打开证据评审</button>}</>;
    }
    render(<Harness />);
    if (phase === "read") {
      await ready(); fireEvent.click(screen.getByRole("button", { name: "读取所选资料原文" }));
      await waitFor(() => expect(calls("/originals/kb-one")).toHaveLength(1));
    } else {
      await read(); fireEvent.click(screen.getByRole("button", { name: "准备待证据评审草稿" }));
      await waitFor(() => expect(calls("/prepare-review")).toHaveLength(1));
    }
    const signal = calls(phase === "read" ? "/originals/kb-one" : "/prepare-review")[0][1]?.signal;
    fireEvent.click(screen.getByRole("button", { name: "关闭证据评审" }));
    expect(signal?.aborted).toBe(true);
    expect(input.onClose).toHaveBeenCalledTimes(1);
    expect(input.onPrepared).toHaveBeenCalledTimes(phase === "prepare" ? 1 : 0);
    fireEvent.click(screen.getByRole("button", { name: "重新打开证据评审" }));
    await ready();
    await act(async () => pending.resolve(phase === "read" ? originals() : prepared()));
    expect(screen.queryByRole("region", { name: "证据原文选择" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "评审草稿已保存" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "读取所选资料原文" })).toBeEnabled();
    expect(input.onPrepared).toHaveBeenCalledTimes(phase === "prepare" ? 1 : 0);
  });
});

describe("native evidence request and receipt failures", () => {
  it.each(["status", "session"] as const)("shows %s read failures and leaves a fresh read available", async phase => {
    installTransport(phase === "status" ? { status: () => Promise.reject(new Error("状态连接失败")) }
      : { session: () => Promise.reject(new Error("证据包创建失败")) });
    render(<NativeEvidenceReview {...props()} />); await ready();
    fireEvent.click(screen.getByRole("button", { name: "读取所选资料原文" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(phase === "status" ? "状态连接失败" : "证据包创建失败");
    expect(screen.getByRole("button", { name: "读取所选资料原文" })).toBeEnabled();
    expect(screen.queryByRole("region", { name: "证据原文选择" })).not.toBeInTheDocument();
  });

  it.each([
    ["network", () => Promise.reject(new Error("草稿保存连接中断")), "草稿保存连接中断"],
    ["semantic claim", () => ({ ...prepared(), semantic_validation: "PERFORMED" }), "草稿回执不完整"],
    ["human acceptance", () => ({ ...prepared(), human_acceptance: "CLAIMED" }), "草稿回执不完整"],
    ["model job", () => ({ ...prepared(), job_id: "model-job" }), "草稿回执不完整"],
    ["assessment hash", () => ({ ...prepared(), assessment_sha256: "invalid" }), "草稿回执不完整"],
  ])("reconciles the ledger after a %s preparation failure without claiming success", async (_name, response, message) => {
    installTransport({ prepare: response }); const input = props();
    render(<NativeEvidenceReview {...input} />); await read();
    fireEvent.click(screen.getByRole("button", { name: "准备待证据评审草稿" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByRole("alert")).toHaveTextContent("先同步工作账本；已有草稿可能已保存");
    expect(input.onPrepared).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "评审草稿已保存" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "准备待证据评审草稿" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "读取所选资料原文" })).toBeEnabled();
  });

  it("keeps source-only reads inactive in a read-only host", async () => {
    installTransport();
    render(<WorkbenchHostContext.Provider value={{ readOnly: true, nativeEvidence: true, resourceUrl: path => path, pdfViewerUrl: () => "" }}>
      <NativeEvidenceReview {...props()} />
    </WorkbenchHostContext.Provider>);
    await ready(); fireEvent.click(screen.getByRole("button", { name: "读取所选资料原文" }));
    expect(calls("/sessions")).toHaveLength(0);
    expect(calls("/status")).toHaveLength(0);
    expect(screen.queryByText("登记项目证据原件")).not.toBeInTheDocument();
  });
});
