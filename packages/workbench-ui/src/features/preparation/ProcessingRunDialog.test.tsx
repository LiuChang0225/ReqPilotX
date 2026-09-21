import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type {
  ProjectProcessingProfile,
  RuntimeCapability,
} from "../../shared/api/contracts";
import { CAPABILITY_IDS } from "../../shared/runtime/processingCapabilities";
import { ProcessingRunDialog } from "./ProcessingRunDialog";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function showModal() {
    Object.defineProperty(this, "open", {
      configurable: true,
      value: true,
      writable: true,
    });
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function close() {
    this.open = false;
    this.removeAttribute("open");
  };
});

afterEach(cleanup);

const profile: ProjectProcessingProfile = {
  schema_version: "project-processing-profile/v1",
  project_id: "project-1",
  document: {
    primary_parser: "azure_document_intelligence",
    enhancement_chain: [],
    complex_vision: "bailian_vision",
    auto_escalation: true,
  },
  llm_provider: "aliyun",
  knowledge_backend: "aliyun_qdrant",
  revision: 3,
  canonical_sha256: "a".repeat(64),
};

function localCapability(state: RuntimeCapability["state"]): RuntimeCapability {
  return {
    capability_id: CAPABILITY_IDS.pymupdf,
    category: "document_parser",
    provider: "local",
    label: "PyMuPDF",
    state,
    ready: state === "READY",
    reason_code: state === "READY" ? "" : "not_available",
    message: state === "READY" ? "" : "本地捕获不可用",
  };
}

function codexCapability(state: RuntimeCapability["state"]): RuntimeCapability {
  return {
    capability_id: CAPABILITY_IDS.codexAgent,
    category: "llm",
    provider: "codex",
    label: "Codex Agent",
    state,
    ready: state === "READY",
    reason_code: state === "READY" ? "" : "codex_unavailable",
    message: state === "READY" ? "" : "Codex Agent 不可用",
  };
}

describe("ProcessingRunDialog automatic Codex path", () => {
  it("removes provider selectors and submits the fixed automatic override", async () => {
    const onStart = vi.fn();
    render(
      <ProcessingRunDialog
        open
        profile={profile}
        capabilities={[localCapability("READY"), codexCapability("READY")]}
        busy={false}
        onClose={() => undefined}
        onStart={onStart}
      />,
    );

    expect(screen.getByRole("heading", { name: "Codex Agent 自动方案" })).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByText("Azure Document Intelligence")).not.toBeInTheDocument();
    expect(screen.getByText(/需求识别与精确拆分 Skills/)).toBeInTheDocument();
    expect(screen.getByText(/模型只提交 Proposal/)).toBeInTheDocument();

    await userEvent.setup().click(
      screen.getByRole("button", { name: "开始自动解析" }),
    );
    expect(onStart).toHaveBeenCalledWith({
      schema_version: "run-profile-override/v1",
      document: {
        primary_parser: "pymupdf",
        enhancement_chain: [],
        complex_vision: null,
        auto_escalation: true,
      },
      llm_provider: "codex",
      force_visual_pages: [],
    });
  });

  it("blocks start when deterministic source capture is unavailable", () => {
    render(
      <ProcessingRunDialog
        open
        profile={profile}
        capabilities={[
          localCapability("UNREACHABLE"),
          codexCapability("READY"),
        ]}
        busy={false}
        onClose={() => undefined}
        onStart={() => undefined}
      />,
    );

    expect(screen.getByText("确定性捕获当前不可用")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始自动解析" })).toBeDisabled();
  });
});
