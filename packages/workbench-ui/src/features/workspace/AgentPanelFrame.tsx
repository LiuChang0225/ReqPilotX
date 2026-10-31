import type { ReactNode } from "react";
import { Icon } from "../../shared/ui/Icon";
import { useUiCopy } from "../../host/HostLocaleContext";

/** The same collaboration surface for native execution and external producer receipts. */
export function AgentPanelFrame({ id, embedded = true, hidden = false, onClose, children, footer }: {
  id?: string; embedded?: boolean; hidden?: boolean; onClose: () => void; children: ReactNode; footer: ReactNode;
}) {
  const l = useUiCopy();
  return <section id={id} hidden={hidden} className={`source-agent-loop-panel${embedded ? " is-embedded" : ""}`}
    role={embedded ? "region" : "dialog"} aria-modal={embedded ? undefined : false} aria-label={l("agentFrame.aria", "Agent 连续任务")}
    onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); } }}>
    <header className="agent-companion-header"><div><Icon name="spark" size={18}/><strong>{l("work.agentCollaboration", "Agent 协作")}</strong><span>{l("agentFrame.subtitle", "与原文一起工作")}</span></div>
      <button type="button" className="icon-button" aria-label={l("agentFrame.close", "关闭 Agent 协作")} onClick={onClose}><Icon name="close" size={16}/></button></header>
    <div className="agent-companion-scroll">{children}</div><footer className="agent-composer-area">{footer}</footer>
  </section>;
}
