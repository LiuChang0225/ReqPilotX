import type {
  DocumentProcessingProfile,
  ProjectProcessingProfile,
  RunProfileOverride,
  RuntimeCapability,
} from "../../shared/api/contracts";
import { profileGate } from "../../shared/runtime/processingCapabilities";
import { Dialog } from "../../shared/ui/Dialog";
import { InlineNotice, StatusBadge } from "../../shared/ui/Feedback";
import { Icon } from "../../shared/ui/Icon";

interface ProcessingRunDialogProps {
  open: boolean;
  profile: ProjectProcessingProfile | null;
  capabilities: RuntimeCapability[];
  busy: boolean;
  onClose: () => void;
  onStart: (override: RunProfileOverride) => void;
}

const AUTOMATIC_DOCUMENT_PROFILE: DocumentProcessingProfile = {
  primary_parser: "pymupdf",
  enhancement_chain: [],
  complex_vision: null,
  auto_escalation: true,
};

export function ProcessingRunDialog({
  open,
  profile,
  capabilities,
  busy,
  onClose,
  onStart,
}: ProcessingRunDialogProps) {
  const gate = profileGate(AUTOMATIC_DOCUMENT_PROFILE, "codex", capabilities);

  const start = () => {
    onStart({
      schema_version: "run-profile-override/v1",
      document: AUTOMATIC_DOCUMENT_PROFILE,
      llm_provider: "codex",
      force_visual_pages: [],
    });
  };

  return (
    <Dialog
      open={open}
      title="开始自动解析"
      onClose={onClose}
      className="processing-run-dialog"
    >
      <header className="modal-header">
        <div>
          <p className="section-label">本次运行</p>
          <h2>Codex Agent 自动方案</h2>
          <p>
            不再选择主解析器或模型供应商。ReqPilotX 先完成确定性来源捕获，再由 Codex Agent Skills 理解和修订结构。
          </p>
        </div>
        <button
          className="icon-button"
          type="button"
          aria-label="关闭自动解析"
          onClick={onClose}
        >
          <Icon name="close" />
        </button>
      </header>

      <div className="processing-run-grid">
        <section className="run-plan-summary" aria-label="自动解析步骤">
          <header>
            <strong>确定性来源捕获</strong>
            <StatusBadge tone="success">自动</StatusBadge>
          </header>
          <p>保留 PDF、DOCX、XLSX 的原文、顺序、页码、表格和坐标，生成不可变 SourceIR。</p>
        </section>
        <section className="run-plan-summary">
          <header>
            <strong>Codex Agent Skills</strong>
            <StatusBadge tone="success">自动</StatusBadge>
          </header>
          <p>按文档事实调用 PDF 理解、Structure、需求识别与精确拆分 Skills；模型只提交 Proposal，不直接写入正式结构或需求。</p>
        </section>
        <section className="run-plan-summary">
          <header>
            <strong>宿主校验与人工 Apply</strong>
            <StatusBadge tone="neutral">受控</StatusBadge>
          </header>
          <p>来源绑定、结构合法性和 Diff 由 ReqPilotX 校验，只有人工确认后才 Apply。</p>
        </section>
      </div>

      {!profile ? (
        <InlineNotice title="项目状态尚未加载" tone="warning">
          刷新项目后再启动文档整理。
        </InlineNotice>
      ) : null}
      {profile && !gate.ready ? (
        <InlineNotice title="确定性捕获当前不可用" tone="warning">
          {gate.message}
        </InlineNotice>
      ) : null}

      <footer className="modal-actions">
        <button className="button ghost" type="button" onClick={onClose}>
          取消
        </button>
        <button
          className="button primary"
          type="button"
          disabled={busy || !profile || !gate.ready}
          onClick={start}
        >
          <Icon name="play" />
          开始自动解析
        </button>
      </footer>
    </Dialog>
  );
}
