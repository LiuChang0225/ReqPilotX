import { Tabs } from "../../shared/ui/Tabs";
import type { ExtractionCheckpoint } from "../workspace/route";
import { useUiCopy } from "../../host/HostLocaleContext";
import { cx } from "../../shared/lib/format";

export type RequirementWorkbenchView = ExtractionCheckpoint | "analysis";
interface RequirementTreeStageRailProps {
  value: RequirementWorkbenchView; reviewAvailable: boolean; workingLedger?: boolean;
  itemsAvailable?: boolean; onChange: (view: RequirementWorkbenchView) => void;
}
/** Inspection lenses inside extraction; the application owns the single major workflow. */
export function RequirementTreeStageRail({ value, reviewAvailable, workingLedger = false, itemsAvailable = true, onChange }: RequirementTreeStageRailProps) {
  const l = useUiCopy();
  const visibleValue = value === "diagnose" ? "structure" : value;
  return <div className="requirement-view-switch is-compact-lens">
    <Tabs className="tree-mode-switch" label={l("workbench.view.switch", "文档视图")} value={visibleValue} onChange={onChange}
      items={[
        { value: "structure" as const, ariaLabel: l("workbench.view.structure", "原文结构"), label: l("workbench.view.structure", "原文结构") },
        { value: "list" as const, ariaLabel: l("workbench.view.items", "原文条目"), label: l("workbench.view.items", "原文条目"), disabled: !itemsAvailable,
          title: itemsAvailable ? l("核对已保存的原文条目") : l("条目尚未保存，当前可先核对结构") },
        { value: "requirements" as const, ariaLabel: l("workbench.view.requirements", "需求清单"), label: l("workbench.view.requirements", "需求清单"), disabled: !itemsAvailable,
          title: l("仅查看已识别需求，保留原文祖先路径") },
      ]} />
    {!workingLedger && <button type="button" className={cx("tree-mode-review-entry", visibleValue === "review" && "is-active")}
      disabled={!reviewAvailable} aria-pressed={visibleValue === "review"} onClick={() => onChange("review")}
      title={reviewAvailable ? l("打开已锁定版本；人工确认不阻止工作基线继续") : l("先在原文条目阶段锁定一个版本")}>{l("workbench.view.review", "版本确认（可选）")}</button>}
  </div>;
}
