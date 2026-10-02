import { useEffect, useReducer, useRef, useState } from "react";
import { api } from "../../shared/api/client";
import { knowledgeGate } from "../../shared/runtime/processingCapabilities";
import { EmptyState, InlineNotice } from "../../shared/ui/Feedback";
import { Dialog } from "../../shared/ui/Dialog";
import { FileName } from "../../shared/ui/FileName";
import { Icon } from "../../shared/ui/Icon";
import { ProjectDocumentLibrary } from "./ProjectDocumentLibrary";
import { useWorkspace } from "../workspace/WorkspaceContext";
import { useWorkbenchHost } from "../workspace/WorkbenchHostContext";
import { UploadQueueDialog } from "../documents/UploadQueueDialog";
import {
  createUploadItems,
  uploadQueueReducer,
  type UploadKind,
} from "../documents/uploadQueue";

export function ProjectManagementPanel() {
  const host = useWorkbenchHost();
  const nativeEvidence = host.nativeEvidence === true && !host.readOnly;
  const {
    projects,
    route,
    updateRoute,
    selectedProject,
    documents,
    runtimeCapabilities,
    processingProfile,
    runAction,
    busyAction,
    refreshProject,
    setNotice,
  } = useWorkspace();
  const sorInput = useRef<HTMLInputElement>(null);
  const knowledgeInput = useRef<HTMLInputElement>(null);
  const uploadControllers = useRef(new Map<string, AbortController>());
  const currentScope = useRef(route);
  currentScope.current = route;
  const [activeKind, setActiveKind] = useState<UploadKind>("sor");
  const [showCreateProject, setShowCreateProject] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [customerName, setCustomerName] = useState("");
  const [uploadQueue, dispatchUpload] = useReducer(uploadQueueReducer, []);
  const [showUploadQueue, setShowUploadQueue] = useState(false);
  const [removalTarget, setRemovalTarget] = useState<{
    kind: UploadKind;
    projectId: string;
    id: string;
    fileName: string;
  } | null>(null);
  const currentKnowledgeGate = knowledgeGate(
    processingProfile,
    runtimeCapabilities,
  );
  const knowledgeReady = currentKnowledgeGate.ready;
  const uploadFiles = (kind: UploadKind, files: FileList | null) => {
    if (!files?.length || !route.projectId) return;
    const result = createUploadItems(
      route.projectId,
      kind,
      Array.from(files),
      uploadQueue.filter(item => item.projectId === route.projectId),
    );
    if (result.items.length) {
      dispatchUpload({ type: "append", items: result.items });
      setShowUploadQueue(true);
    }
    if (result.rejected.length) {
      setNotice(result.rejected.slice(0, 2).join("；"));
    }
  };

  const createProject = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busyAction || !projectName.trim() || !customerName.trim()) return;
    const created = await runAction(
      "创建项目",
      () => api.createProject(projectName.trim(), customerName.trim()),
      { refresh: "all" },
    );
    if (!created) return;
    setProjectName("");
    setCustomerName("");
    setShowCreateProject(false);
    updateRoute(
      {
        task: "projects",
        projectId: created.id,
        documentId: "",
        requirementSetId: "",
      },
      { replace: true },
    );
  };

  useEffect(() => {
    const active = uploadQueue.find((item) =>
      ["uploading", "verifying"].includes(item.status),
    );
    if (active) return;
    const next = uploadQueue.find((item) => item.status === "queued" && item.projectId === route.projectId);
    if (!next || uploadControllers.current.has(next.id)) return;
    const controller = new AbortController();
    uploadControllers.current.set(next.id, controller);
    dispatchUpload({ type: "start", id: next.id });
    const options = {
      signal: controller.signal,
      onProgress: (progress: number) =>
        dispatchUpload({ type: "progress", id: next.id, progress }),
      onTransferred: () => dispatchUpload({ type: "verify", id: next.id }),
    };
    void (
      next.kind === "sor"
        ? api.uploadSorDocument(next.projectId, next.file, options)
        : api.uploadKnowledgeDocument(next.projectId, next.file, options)
    )
      .then(async () => {
        dispatchUpload({ type: "succeed", id: next.id });
        if (next.projectId === currentScope.current.projectId) {
          try { await refreshProject(); }
          catch { setNotice(`${next.file.name} 已保存，文档列表暂未更新。请同步项目后查看。`); }
        }
      })
      .catch((cause) => {
        if ((cause as { name?: string }).name === "AbortError") {
          dispatchUpload({ type: "cancel", id: next.id });
          return;
        }
        dispatchUpload({
          type: "fail",
          id: next.id,
          error: cause instanceof Error ? cause.message : String(cause),
        });
      })
      .finally(() => {
        uploadControllers.current.delete(next.id);
      });
  }, [refreshProject, route.projectId, uploadQueue, setNotice]);

  useEffect(
    () => () => {
      uploadControllers.current.forEach((controller) => controller.abort());
    },
    [],
  );

  useEffect(() => {
    for (const item of uploadQueue) {
      if (
        item.projectId !== route.projectId &&
        ["queued", "uploading"].includes(item.status)
      ) {
        const controller = uploadControllers.current.get(item.id);
        if (controller) controller.abort();
        else dispatchUpload({ type: "cancel", id: item.id });
      }
    }
  }, [route.projectId, uploadQueue]);

  const cancelUpload = (id: string) => {
    const controller = uploadControllers.current.get(id);
    if (controller) controller.abort();
    else dispatchUpload({ type: "cancel", id });
  };

  const removeSor = async (projectId: string, documentId: string) => {
    const result = await runAction(
      "移除客户来源",
      () => api.deleteSorDocument(projectId, documentId),
      { refresh: "project" },
    );
    if (result && projectId === currentScope.current.projectId && documentId === currentScope.current.documentId) {
      const next = documents.find((item) => item.id !== documentId);
      updateRoute(
        {
          documentId: next?.id || "",
          requirementSetId: "",
        },
        { replace: true },
      );
    }
    return Boolean(result);
  };

  const removeKnowledge = async (projectId: string, documentId: string) => {
    const result = await runAction(
      "移除知识依据",
      () => api.deleteKnowledgeDocument(projectId, documentId),
      { refresh: "project" },
    );
    return Boolean(result);
  };

  const confirmRemoval = async () => {
    if (!removalTarget || busyAction || removalTarget.projectId !== route.projectId) return;
    const removed =
      removalTarget.kind === "sor"
        ? await removeSor(removalTarget.projectId, removalTarget.id)
        : await removeKnowledge(removalTarget.projectId, removalTarget.id);
    if (removed) setRemovalTarget(current => current === removalTarget ? null : current);
  };

  useEffect(() => { setRemovalTarget(null); }, [route.projectId]);

  return (
    <section className="management-page project-management-page">
      <header className="page-command-header">
        <div>
          <p className="section-label">项目、来源与知识依据</p>
          <h1>项目管理</h1>
          <p>
            {selectedProject ? `当前项目：${selectedProject.name} · ${selectedProject.customer_name}` : "创建项目，或选择已有项目继续工作。"}
          </p>
        </div>
        <div className="command-actions">
          {uploadQueue.length > 0 && (
            <button
              className="button ghost"
              type="button"
              onClick={() => setShowUploadQueue(true)}
            >
              <Icon name="upload" />
              上传队列
              <span>
                {
                  uploadQueue.filter((item) =>
                    ["queued", "uploading", "verifying"].includes(item.status),
                  ).length
                }
              </span>
            </button>
          )}
          <button
            className="button secondary"
            type="button"
            onClick={() => setShowCreateProject(true)}
          >
            <Icon name="plus" />
            新建项目
          </button>
          <button
            className="button primary"
            type="button"
            disabled={!selectedProject}
            onClick={() => {
              setActiveKind("sor");
              sorInput.current?.click();
            }}
          >
            <Icon name="upload" />
            添加客户来源
          </button>
          <button
            className="button secondary"
            type="button"
            disabled={!selectedProject}
            title={
              nativeEvidence ? "登记证据原件，不运行索引或模型" : knowledgeReady
                ? "添加知识依据"
                : "文件会先安全保存；检索能力连接后再同步"
            }
            onClick={() => {
              if (nativeEvidence) {
                updateRoute({ task: "knowledge", knowledgeSpace: "project", knowledgePage: undefined, knowledgeView: undefined });
                return;
              }
              setActiveKind("knowledge");
              knowledgeInput.current?.click();
            }}
          >
            <Icon name="plus" />
            添加知识依据
          </button>
          <input
            ref={sorInput}
            hidden
            type="file"
            multiple
            accept=".pdf,.docx,.xlsx,.txt"
            onChange={(event) => {
              uploadFiles("sor", event.target.files);
              event.target.value = "";
            }}
          />
          <input
            ref={knowledgeInput}
            hidden
            type="file"
            multiple
            accept=".pdf,.docx,.xlsx,.txt"
            onChange={(event) => {
              uploadFiles("knowledge", event.target.files);
              event.target.value = "";
            }}
          />
        </div>
      </header>

      <details className="source-guidance-disclosure"><summary>来源与处理说明</summary><div className="source-guidance">
        {processingProfile && !knowledgeReady && (
          <InlineNotice
            title={
              processingProfile.knowledge_backend === "none"
                ? "知识文件可先保存，检索按需启用"
                : "知识文件可先保存，检索待连接"
            }
            tone="info"
          >
            {processingProfile.knowledge_backend === "none"
              ? "当前项目未启用知识检索；这不会阻断项目、来源或需求工作流。需要检索时再由管理员启用。"
              : "当前项目选择的知识检索后端尚未就绪；文件会先保存，连接完成后再同步。"}
          </InlineNotice>
        )}

        <p className="source-format-note">
          PDF 可逐页核对；DOCX、XLSX 与 TXT 可导入、提取内容并打开原文件。
        </p>
      </div>

      </details>

      {!selectedProject && <EmptyState icon="folder" title={projects.length ? "选择项目以查看文档" : "创建第一个项目"}
        description={projects.length ? "使用下方项目选择器继续已有工作，或新建项目。" : "先为客户文档建立项目，随后添加来源并设置 Agent 任务。"}
        action={<button type="button" className="button primary" onClick={() => setShowCreateProject(true)}>创建项目</button>} />}
      {(selectedProject || projects.length > 0) && <ProjectDocumentLibrary kind={activeKind} onKindChange={setActiveKind} onRemove={(kind, id, fileName) => setRemovalTarget({ kind, projectId: route.projectId, id, fileName })} />}

      <UploadQueueDialog
        open={showUploadQueue}
        items={uploadQueue}
        currentProjectId={route.projectId}
        onClose={() => setShowUploadQueue(false)}
        onCancel={cancelUpload}
        onRetry={(id) => {
          const item = uploadQueue.find(value => value.id === id);
          if (item?.projectId !== route.projectId) { setNotice("请先切回该文件所属项目，再重试上传。"); return; }
          dispatchUpload({ type: "retry", id });
        }}
        onRemove={(id) => dispatchUpload({ type: "remove", id })}
        onClearFinished={() => dispatchUpload({ type: "clear-finished" })}
      />
      <Dialog
        open={showCreateProject}
        title="创建项目"
        onClose={() => setShowCreateProject(false)}
      >
        <header>
          <div>
            <p className="section-label">新建项目</p>
            <h2>创建项目</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭"
            onClick={() => setShowCreateProject(false)}
          >
            <Icon name="close" />
          </button>
        </header>
        <form
          className="dialog-form"
          onSubmit={(event) => void createProject(event)}
        >
          <label>
            <span>项目名称</span>
            <input
              autoFocus
              required
              value={projectName}
              onChange={(event) => setProjectName(event.target.value)}
              placeholder="例如：BMS Platform Gen3"
            />
          </label>
          <label>
            <span>客户名称</span>
            <input
              required
              value={customerName}
              onChange={(event) => setCustomerName(event.target.value)}
              placeholder="例如：OEM A"
            />
          </label>
          <footer>
            <button
              className="button ghost"
              type="button"
              onClick={() => setShowCreateProject(false)}
            >
              取消
            </button>
            <button className="button primary" type="submit" disabled={Boolean(busyAction) || !projectName.trim() || !customerName.trim()}>
              {busyAction === "创建项目" ? "正在创建…" : "创建并进入"}
            </button>
          </footer>
        </form>
      </Dialog>
      <Dialog
        open={Boolean(removalTarget)}
        title="移除来源"
        className="confirm-action-dialog"
        onClose={() => setRemovalTarget(null)}
      >
        <header>
          <div>
            <p className="section-label">不可撤销操作</p>
            <h2>
              移除
              {removalTarget?.kind === "sor" ? "客户来源" : "知识依据"}
            </h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭"
            onClick={() => setRemovalTarget(null)}
          >
            <Icon name="close" />
          </button>
        </header>
        <p className="confirm-action-copy">
          将从当前项目移除
          {removalTarget && <FileName value={removalTarget.fileName} />}。
          已关联的处理结果将不能继续从界面访问。
        </p>
        <footer className="confirm-action-footer">
          <button
            className="button ghost"
            type="button"
            onClick={() => setRemovalTarget(null)}
          >
            取消
          </button>
          <button
            className="button danger"
            type="button"
            disabled={Boolean(busyAction)}
            onClick={() => void confirmRemoval()}
          >
            {busyAction === "移除客户来源" || busyAction === "移除知识依据" ? "正在移除…" : "确认移除"}
          </button>
        </footer>
      </Dialog>
    </section>
  );
}
