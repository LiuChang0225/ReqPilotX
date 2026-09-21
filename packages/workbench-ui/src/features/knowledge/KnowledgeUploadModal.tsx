import { useEffect, useRef, useState } from "react";
import { Dialog } from "../../shared/ui/Dialog";
import type { KnowledgeNode, KnowledgeLink } from "./knowledgeData";
import { Icon } from "../../shared/ui/Icon";

interface KnowledgeUploadModalProps {
  isOpen: boolean;
  onClose: () => void;
  onIngestSuccess: (
    newNode: KnowledgeNode,
    newLinks: KnowledgeLink[],
    rawMarkdown: string
  ) => void;
}

export function KnowledgeUploadModal({
  isOpen,
  onClose,
  onIngestSuccess,
}: KnowledgeUploadModalProps) {
  const [title, setTitle] = useState("");
  const [category, setCategory] = useState<string>("specs");
  const [tags, setTags] = useState("");
  const [markdownContent, setMarkdownContent] = useState("");
  const [isProcessing, setIsProcessing] = useState(false);

  const [error, setError] = useState("");
  const activeReader = useRef<FileReader | null>(null);
  useEffect(() => () => { activeReader.current?.abort(); }, []);

  if (!isOpen) return null;

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    activeReader.current?.abort();
    setIsProcessing(false); setError("");
    const isMarkdownOrText = /\.(md|txt)$/i.test(file.name);
    if (!isMarkdownOrText) {
      setError("此预览入口支持 .md / .txt 文件，请重新选择；当前输入仍保留。");
      return;
    }

    setIsProcessing(true);

    const reader = new FileReader();
    activeReader.current = reader;
    reader.onerror = () => { if (activeReader.current === reader) { setError("文件读取失败，请重新选择文件或直接填写正文。"); setIsProcessing(false); } };
    reader.onabort = () => { if (activeReader.current === reader) setIsProcessing(false); };
    reader.onload = (event) => {
      if (activeReader.current !== reader) return;
      setTitle(file.name.replace(/\.[^/.]+$/, ""));
      const text = (event.target?.result as string) || "";
      setMarkdownContent(
        text.startsWith("---")
          ? text
          : `---\ntitle: ${file.name.replace(/\.[^/.]+$/, "")}\ncategory: ${category}\ntags: [新规范, 导入]\nlast_updated: ${new Date().toISOString().slice(0, 10)}\n---\n\n# ${file.name.replace(/\.[^/.]+$/, "")}\n\n${text}`
      );
      setIsProcessing(false);
    };
    reader.readAsText(file);
    e.currentTarget.value = "";
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim() || !markdownContent.trim() || isProcessing) return;

    const safeId = `${category}/${crypto.randomUUID()}`;
    const tagList = tags
      .split(/[,，]/)
      .map((t) => t.trim())
      .filter(Boolean);

    // Extract any [[links]] from markdown
    const linkRegex = /\[\[(.*?)\]\]/g;
    const detectedTargets: string[] = [];
    let match: RegExpExecArray | null;
    while ((match = linkRegex.exec(markdownContent)) !== null) {
      detectedTargets.push(match[1].split("|")[0].trim());
    }


    const newNode: KnowledgeNode = {
      id: safeId,
      name: title,
      category,
      tags: tagList.length ? tagList : ["本次预览"],
      path: `${safeId}.md`,
      val: 10,
      color:
        category === "specs"
          ? "#3b82f6"
          : category === "ee_hardware"
          ? "#8b5cf6"
          : category === "software_diagnostics"
          ? "#06b6d4"
          : category === "dvp_tests"
          ? "#10b981"
          : category === "interfaces"
          ? "#f59e0b"
          : "#ec4899",
    };

    const newLinks: KnowledgeLink[] = detectedTargets.map((target) => ({
      source: safeId,
      target,
      value: 1,
    }));

    onIngestSuccess(newNode, newLinks, markdownContent);
    setTitle(""); setTags(""); setMarkdownContent(""); setError("");
    onClose();
  };

  return <Dialog open={isOpen} title="添加本次预览资料" onClose={onClose}>
    <section data-testid="knowledge-upload-modal" style={{ maxHeight: "calc(100dvh - 80px)", overflowY: "auto" }}>
      <header style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
        <h2 style={{ margin: 0, fontSize: 17 }}>添加本次预览资料</h2>
        <button type="button" className="icon-button" aria-label="关闭资料预览" onClick={onClose}><Icon name="close" size={16} /></button>
      </header>
      <p>资料仅加入当前页面的知识图谱预览，刷新后不会保留；未上传或写入项目知识库。</p>
      {error && <p role="alert">{error}</p>}
      <form onSubmit={handleSubmit} style={{ display: "grid", gap: 14 }}>
        <label style={{ display: "grid", gap: 6 }}>选择 Markdown 或文本文件
          <input type="file" accept=".md,.txt" onChange={handleFileUpload} />
        </label>
        <label style={{ display: "grid", gap: 6 }}>词条/规范名称
          <input type="text" required value={title} onChange={e => setTitle(e.target.value)} placeholder="例如: PROJ-MOTOR-A 电涡流定子技术协议补遗" />
        </label>
        <label style={{ display: "grid", gap: 6 }}>所属分类
          <select value={category} onChange={e => setCategory(e.target.value)}>
            <option value="specs">规格包线</option><option value="ee_hardware">电子电气硬件</option>
            <option value="software_diagnostics">软件与诊断</option><option value="dvp_tests">DVP 测试</option>
            <option value="interfaces">接口规范</option><option value="integration_rules">设计准则</option>
          </select>
        </label>
        <label style={{ display: "grid", gap: 6 }}>标签（逗号分隔）
          <input type="text" value={tags} onChange={e => setTags(e.target.value)} placeholder="例如: OEM-A, 定子, 补遗" />
        </label>
        <label style={{ display: "grid", gap: 6 }}>Markdown 正文（支持双链 [[节点名]]）
          <textarea rows={4} required value={markdownContent} onChange={e => setMarkdownContent(e.target.value)} placeholder="填写或选择文件读取正文" style={{ resize: "vertical", width: "100%" }} />
        </label>
        {isProcessing && <p role="status">正在读取本地文件…</p>}
        <footer style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
          <button type="button" className="button secondary" onClick={onClose}>取消</button>
          <button type="submit" className="button primary" disabled={!title.trim() || !markdownContent.trim() || isProcessing}>加入本次预览</button>
        </footer>
      </form>
    </section>
  </Dialog>;
}
