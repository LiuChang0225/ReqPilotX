import type { ReactNode } from "react";
import { Icon } from "../../shared/ui/Icon";

/** The same collaboration surface for native execution and external producer receipts. */
export function AgentPanelFrame({ id, embedded = true, hidden = false, onClose, children, footer }: {
  id?: string; embedded?: boolean; hidden?: boolean; onClose: () => void; children: ReactNode; footer: ReactNode;
}) {
  return <section id={id} hidden={hidden} className={`source-agent-loop-panel${embedded ? " is-embedded" : ""}`}
    role={embedded ? "region" : "dialog"} aria-modal={embedded ? undefined : false} aria-label="Agent 连续任务"
    onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); } }}>
    <header className="agent-companion-header"><div><Icon name="spark" size={18}/><strong>Agent 协作</strong><span>与原文一起工作</span></div>
      <button type="button" className="icon-button" aria-label="关闭 Agent 协作" onClick={onClose}><Icon name="close" size={16}/></button></header>
    <div className="agent-companion-scroll">{children}</div><footer className="agent-composer-area">{footer}</footer>
  </section>;
}
