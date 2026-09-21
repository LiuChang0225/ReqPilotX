import { Tabs } from "../../shared/ui/Tabs";
import type { ExtractionCheckpoint } from "../workspace/route";

interface RequirementTreeStageRailProps {
  value: ExtractionCheckpoint;
  reviewAvailable: boolean;
  workingLedger?: boolean;
  itemsAvailable?: boolean;
  onChange: (checkpoint: ExtractionCheckpoint) => void;
}

export function RequirementTreeStageRail({
  value,
  reviewAvailable,
  workingLedger = false,
  itemsAvailable = true,
  onChange,
}: RequirementTreeStageRailProps) {
  const visibleValue = value === "diagnose" ? "structure" : value;

  return (
    <Tabs
      className="tree-mode-switch"
      label="需求提取步骤"
      value={visibleValue}
      onChange={onChange}
      items={[
        {
          value: "structure" as const,
          ariaLabel: "第 1 步：原文结构",
          label: (
            <>
              <span className="tree-mode-step-index" aria-hidden="true">
                1
              </span>
              <span className="tree-mode-step-label">原文结构</span>
            </>
          ),
        },
        {
          value: "list" as const,
          disabled: !itemsAvailable,
          title: itemsAvailable ? "核对已保存的原文条目与分类" : "条目尚未保存，当前可先核对结构",
          ariaLabel: "第 2 步：原文需求清单",
          label: (
            <>
              <span className="tree-mode-step-index" aria-hidden="true">
                2
              </span>
              <span className="tree-mode-step-label">原文需求清单</span>
            </>
          ),
        },
        {
          value: "review" as const,
          ariaLabel: reviewAvailable
            ? "第 3 步：版本确认（可选）"
            : "第 3 步：版本确认（可选）；锁定版本后可用",
          disabled: !reviewAvailable,
          title: reviewAvailable
            ? "打开已锁定版本；人工确认不阻止工作基线继续"
            : "先在原文需求清单中锁定一个版本",
          label: (
            <>
              <span className="tree-mode-step-index" aria-hidden="true">
                3
              </span>
              <span className="tree-mode-step-label">版本确认（可选）</span>
            </>
          ),
        },
      ].filter(item => !workingLedger || item.value !== "review")}
    />
  );
}
