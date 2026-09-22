import { useWorkspace } from "../workspace/WorkspaceContext";
import { Tabs } from "../../shared/ui/Tabs";
import { KnowledgeGraphWorkbench } from "./KnowledgeGraphWorkbench";
import { ProjectKnowledgePanel } from "./ProjectKnowledgePanel";

export function KnowledgeWorkspace() {
  const { route, updateRoute, selectedProject } = useWorkspace();
  const reference = Boolean(route.knowledgeSpace && route.knowledgeSpace !== "project");
  return <section className="knowledge-workspace">
    <Tabs className="knowledge-scope-tabs" label="知识范围" value={reference ? "reference" : "project"}
      items={[{ value: "project", label: "项目依据" }, { value: "reference", label: "参考资料" }]}
      onChange={value => updateRoute({ knowledgeSpace: value === "project" ? "project" : "irps", knowledgePage: undefined, knowledgeView: undefined })}/>
    {reference ? <KnowledgeGraphWorkbench /> : <div className="project-knowledge-page"><header><div><h1>项目依据</h1><p>{selectedProject?.name || "尚未选择项目"} · 用于当前项目的原文阅读与分析引用</p></div><button type="button" className="button secondary compact" onClick={() => updateRoute({ task: "projects" })}>管理项目资料</button></header>
      <ProjectKnowledgePanel projectId={route.projectId} focusedDocumentId={route.knowledgePage} onFocusDocument={knowledgePage => updateRoute({ knowledgeSpace: "project", knowledgePage }, { replace: true })}/>
    </div>}
  </section>;
}
