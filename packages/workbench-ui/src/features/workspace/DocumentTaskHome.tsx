import { Icon } from "../../shared/ui/Icon";
import { useDocumentAgent } from "./DocumentAgentContext";
import { agentTaskLabel, agentProgressLabel, isAgentTaskActive } from "./documentWorkSummary";

export function DocumentTaskHome({ fileName, sourceFileUrl }: { fileName: string; sourceFileUrl: string }) {
  const agent = useDocumentAgent()!;
  const task = agent.task;
  const active = isAgentTaskActive(task);
  const description = !task?.task_id
    ? "原文件已保存。确认协作区的目标与范围后开始，随后在这里核对逐步形成的成果。"
    : active ? agentProgressLabel(task.progress?.message) || "正在处理当前文档，已保存的阶段成果可随时核对。"
    : task.job_status === "FAILED" ? "本轮任务未完成。原文件与已保存的成果仍然保留，可在协作区查看原因并继续处理。"
    : task.job_status === "CANCELLED" || task.runtime_status === "INTERRUPTED" ? "任务已暂停。已有处理记录和成果会保留，可在协作区查看并接续任务。"
    : task.map_confirmation_required ? "结构草案已保存。打开协作区核对结构，再继续整理原文条目。"
    : "已有任务和处理记录会保留。查看任务详情，或补充当前范围的意见。";
  return <section className="document-task-home" aria-label="文档任务简报">
    <div className="document-task-intro">
      <span className="document-task-intro-icon"><Icon name={active ? "refresh" : "spark"} size={23} className={active ? "is-spinning" : ""} /></span>
      <h2>{task?.task_id ? agentTaskLabel(task) : "为这份文档设置任务"}</h2>
      <p role="status">{description}</p>
      {!task?.task_id && <div className="task-output-preview"><strong>目标成果</strong><ul><li><Icon name="tree" size={15} />可核对的文档结构</li><li><Icon name="requirement" size={15} />来源忠实的原文条目与分类</li><li><Icon name="matrix" size={15} />同一版本的 Matrix 草稿</li></ul></div>}
      <button type="button" className="button secondary" onClick={() => agent.show()}><Icon name="spark" size={15} />{task?.task_id ? "查看任务与补充意见" : "编辑任务目标"}</button>
      <a href={sourceFileUrl} target="_blank" rel="noreferrer" className="task-source-link"><Icon name="external" size={14} />打开原文件</a>
    </div>
    <div className="task-source-preview"><header><Icon name="document" size={14} />{fileName}<span>已保存原件</span></header>
      {fileName.toLowerCase().endsWith(".pdf") ? <iframe title="任务原文件预览" src={`/pdf-viewer?embedded=1&fit=page&pdf=${encodeURIComponent(sourceFileUrl)}&page=1`} /> : <p>原文件已保存，可在新窗口打开查看。</p>}
    </div>
  </section>;
}
