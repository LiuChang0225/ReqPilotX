import type {
  EntryClassificationResponse,
  EntryClassificationType,
  RequirementTreeNode,
} from "../../shared/api/contracts";
import {
  ENTRY_CLASSIFICATION_OPTIONS,
  effectiveEntryClassificationType,
  entryClassificationForNode,
  entryClassificationLabel,
} from "./entryClassificationModel";
import { useUiCopy } from "../../host/HostLocaleContext";

export function EntryClassificationInspector({
  node,
  classification,
  busy,
  onChange,
}: {
  node: RequirementTreeNode | null;
  classification: EntryClassificationResponse | null;
  busy: boolean;
  onChange: (
    workflowNodeId: string,
    entryType: EntryClassificationType | null,
  ) => void;
}) {
  const l = useUiCopy();
  const entry = entryClassificationForNode(classification, node?.id || "");
  if (!node) {
    return (
      <section className="entry-classification-inspector" aria-label={l("需求基线判断")}>
        <p>{l("选择一条原文内容后，可核对它是否属于需求以及需求类型。")}</p>
      </section>
    );
  }
  if (!classification) {
    return (
      <section
        className="entry-classification-inspector is-unavailable"
        aria-label={l("需求基线判断")}
      >
        <header>
          <div>
            <span>需求基线判断</span>
            <strong>{l("判断结果暂不可用")}</strong>
          </div>
        </header>
        <p>{l("当前原文仍可浏览；同步判断结果后即可继续核对，不会改写来源内容。")}</p>
      </section>
    );
  }
  if (!entry) {
    return (
      <section className="entry-classification-inspector is-structure" aria-label={l("需求基线判断")}>
        <header>
          <div>
            <span>需求基线判断</span>
            <strong>{l("结构标题不作为需求判断")}</strong>
          </div>
        </header>
        <p>{l("该标题仍会完整保留在基线和导出矩阵中，用于呈现原文件层级。")}</p>
      </section>
    );
  }

  const currentType = effectiveEntryClassificationType(entry);
  const accepted = classification?.snapshot.status === "ACCEPTED";
  return (
    <section className="entry-classification-inspector" aria-label={l("需求基线判断")}>
      <header>
        <div>
          <span>需求基线判断</span>
          <strong>{entryClassificationLabel(currentType, l)}</strong>
        </div>
        <small>
          {accepted
            ? l("本轮分类已接受")
            : entry.human_override
              ? l("人工指定 · 已保存")
              : entry.suggestion_source === "NONE"
                ? l("尚未判断 · 等待人工选择")
                : l("Agent 建议 · 可人工修改")}
        </small>
      </header>
      <p>
        {l("技术需求和非技术需求纳入需求基线；信息 / 参考保留原文但不进入后续需求分析。本步骤不改写、摘要或拆分原文。")}
      </p>
      <div className="entry-classification-options">
        {ENTRY_CLASSIFICATION_OPTIONS.map((option) => (
          <button
            key={option.value}
            type="button"
            aria-label={l(option.label)}
            aria-pressed={currentType === option.value}
            disabled={busy || accepted}
            onClick={() => onChange(entry.workflow_node_id, option.value)}
          >
            <strong>{l(option.label)}</strong>
            <small>{l(option.description)}</small>
          </button>
        ))}
      </div>
      <footer>
        <span>{accepted ? l("分类已锁定") : l("修改会自动保存")}</span>
        {entry.human_override && !accepted ? (
          <button
            type="button"
            className="text-button"
            disabled={busy}
            onClick={() => onChange(entry.workflow_node_id, null)}
          >
            {l("清除人工分类")}
          </button>
        ) : null}
      </footer>
    </section>
  );
}
