import { useState, useMemo, useCallback } from "react";
import {
  KNOWLEDGE_SPACES,
  searchKnowledgeBase,
  type KnowledgeNode,
  type KnowledgeLink,
} from "./knowledgeData";
import { KnowledgeFileTree } from "./KnowledgeFileTree";
import { KnowledgeDocViewer } from "./KnowledgeDocViewer";
import { KnowledgeInspector } from "./KnowledgeInspector";
import { ForceGraphCanvas } from "./ForceGraphCanvas";
import { KnowledgeUploadModal } from "./KnowledgeUploadModal";
import { useWorkspaceRoute } from "../workspace/route";
import { Icon } from "../../shared/ui/Icon";

export function KnowledgeGraphWorkbench() {
  const { route, update: updateRoute } = useWorkspaceRoute();

  // Active space from route
  const rawSpaceId = route.knowledgeSpace || "irps";
  const activeSpace = KNOWLEDGE_SPACES[rawSpaceId] || KNOWLEDGE_SPACES["irps"];
  const activeSpaceId = activeSpace.id; // "irps" or "cus2"

  // Pane width resize state
  const [leftWidth, setLeftWidth] = useState(250);
  const [rightWidth, setRightWidth] = useState(320);

  // Dynamic uploads isolated per space
  const [customSpaceData, setCustomSpaceData] = useState<Record<string, {
    nodes: KnowledgeNode[];
    links: KnowledgeLink[];
    pageContents: Record<string, string>;
  }>>({});

  const currentCustom = useMemo(() => {
    return customSpaceData[activeSpaceId] || { nodes: [], links: [], pageContents: {} };
  }, [customSpaceData, activeSpaceId]);

  const effectiveNodes = useMemo(() => [
    ...activeSpace.graphData.nodes,
    ...currentCustom.nodes,
  ], [activeSpace, currentCustom.nodes]);

  const effectiveLinks = useMemo(() => [
    ...activeSpace.graphData.links,
    ...currentCustom.links,
  ], [activeSpace, currentCustom.links]);

  const effectivePages = useMemo(() => ({
    ...activeSpace.pageContents,
    ...currentCustom.pageContents,
  }), [activeSpace, currentCustom.pageContents]);

  // Active page & view from URL (persistent and strictly validated against current space)
  const activePageId = useMemo(() => {
    if (route.knowledgePage && effectivePages[route.knowledgePage]) {
      return route.knowledgePage;
    }
    return activeSpace.defaultPageId;
  }, [route.knowledgePage, effectivePages, activeSpace]);

  const activeView = route.knowledgeView || "split"; // "doc" | "graph" | "split"

  const [searchQuery, setSearchQuery] = useState("");
  const [highlightedPath, setHighlightedPath] = useState<string[]>([]);
  const [isUploadModalOpen, setIsUploadModalOpen] = useState(false);

  const handleLeftResize = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = leftWidth;
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";

    const onMouseMove = (moveEvent: MouseEvent) => {
      const delta = moveEvent.clientX - startX;
      const newW = Math.max(180, Math.min(460, startW + delta));
      setLeftWidth(newW);
    };

    const onMouseUp = () => {
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };

    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
  }, [leftWidth]);

  const handleRightResize = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = rightWidth;
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";

    const onMouseMove = (moveEvent: MouseEvent) => {
      const delta = startX - moveEvent.clientX;
      const newW = Math.max(220, Math.min(520, startW + delta));
      setRightWidth(newW);
    };

    const onMouseUp = () => {
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };

    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
  }, [rightWidth]);

  const handleSpaceChange = (newSpaceId: string) => {
    const targetSpace = KNOWLEDGE_SPACES[newSpaceId] || KNOWLEDGE_SPACES["irps"];
    updateRoute({
      task: "knowledge",
      knowledgeSpace: targetSpace.id,
      knowledgePage: targetSpace.defaultPageId,
    });
    setHighlightedPath([]);
    setSearchQuery("");
  };

  // ZG hybrid search scoped strictly to effective pages and nodes
  const searchResults = useMemo(() => {
    return searchKnowledgeBase(
      searchQuery,
      6,
      effectivePages,
      effectiveNodes
    );
  }, [searchQuery, effectivePages, effectiveNodes]);

  const handleSelectNode = (targetId: string) => {
    // Normalization check
    const found = effectiveNodes.find(
      (n) => n.id === targetId || n.id.endsWith(`/${targetId}`)
    );
    const resolvedId = found ? found.id : targetId;
    updateRoute({ task: "knowledge", knowledgePage: resolvedId });
    setHighlightedPath([]);
  };

  const handleViewChange = (view: "doc" | "graph" | "split") => {
    updateRoute({ task: "knowledge", knowledgeView: view });
  };

  const handleIngestSuccess = (
    newNode: KnowledgeNode,
    newLinks: KnowledgeLink[],
    rawMarkdown: string
  ) => {
    setCustomSpaceData((prev) => {
      const spaceCustom = prev[activeSpaceId] || { nodes: [], links: [], pageContents: {} };
      return {
        ...prev,
        [activeSpaceId]: {
          nodes: [...spaceCustom.nodes, newNode],
          links: [...spaceCustom.links, ...newLinks],
          pageContents: { ...spaceCustom.pageContents, [newNode.id]: rawMarkdown },
        },
      };
    });
    updateRoute({ knowledgePage: newNode.id });
    setHighlightedPath([]);
  };

  return (
    <div
      data-testid="knowledge-graph-workbench"
      style={{
        display: "flex",
        flexDirection: "column",
        width: "100%",
        height: "100%",
        backgroundColor: "var(--canvas, #f3f5f2)",
        color: "var(--ink, #18211c)",
        overflow: "hidden",
        fontFamily: "var(--font-sans)",
      }}
    >
      {/* 1. Top Workbench Header */}
      <header
        style={{
          minHeight: 50,
          padding: "8px 16px",
          backgroundColor: "var(--paper, #ffffff)",
          borderBottom: "1px solid var(--line, #d9dfda)",
          display: "flex",
          flexWrap: "wrap",
          gap: 10,
          alignItems: "center",
          justifyContent: "space-between",
          zIndex: 20,
        }}
      >
        {/* Left Title & Status */}
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10, minWidth: 0 }}>
          <span style={{ color: "var(--brand, #00893d)" }}>
            <Icon name="knowledge" size={18} />
          </span>
          <h1 style={{ fontSize: "14px", fontWeight: 600, margin: 0 }}>
            {activeSpace.title}
          </h1>
          <select
            aria-label="知识库空间切换"
            value={activeSpaceId}
            onChange={(e) => handleSpaceChange(e.target.value)}
            style={{
              fontSize: "11px",
              padding: "2px 8px",
              borderRadius: "6px",
              border: "1px solid var(--line, #d9dfda)",
              backgroundColor: "var(--canvas, #f3f5f2)",
              color: "var(--ink, #18211c)",
              outline: "none",
              cursor: "pointer",
              fontWeight: 500,
            }}
          >
            <option value="irps">合成装置示例</option>
            <option value="cus2">合成控制器示例</option>
          </select>
          <span
            style={{
              fontSize: "11px",
              padding: "2px 7px",
              borderRadius: "10px",
              backgroundColor: "var(--brand-faint, #f1f8f3)",
              color: "var(--brand-strong, #006d31)",
              fontWeight: 500,
            }}
          >
            {effectiveNodes.length} 篇词条 · {effectiveLinks.length} 条关系网络
          </span>
        </div>

        {/* Center View Switcher (Obsidian style) */}
        <div
          role="group" aria-label="知识库显示方式"
          style={{
            display: "flex",
            backgroundColor: "var(--canvas, #f3f5f2)",
            borderRadius: "6px",
            padding: 2,
            border: "1px solid var(--line, #d9dfda)",
          }}
        >
          <button
            type="button"
            onClick={() => handleViewChange("doc")}
            aria-pressed={activeView === "doc"}
            style={{
              padding: "4px 12px",
              fontSize: "12px",
              borderRadius: "4px",
              border: "none",
              backgroundColor: activeView === "doc" ? "var(--paper, #ffffff)" : "transparent",
              color: activeView === "doc" ? "var(--brand-strong, #006d31)" : "var(--muted, #6f7a73)",
              fontWeight: activeView === "doc" ? 600 : 400,
              boxShadow: activeView === "doc" ? "0 1px 3px rgba(0,0,0,0.08)" : "none",
              cursor: "pointer",
            }}
          >
            📄 词条阅读
          </button>
          <button
            type="button"
            onClick={() => handleViewChange("split")}
            aria-pressed={activeView === "split"}
            style={{
              padding: "4px 12px",
              fontSize: "12px",
              borderRadius: "4px",
              border: "none",
              backgroundColor: activeView === "split" ? "var(--paper, #ffffff)" : "transparent",
              color: activeView === "split" ? "var(--brand-strong, #006d31)" : "var(--muted, #6f7a73)",
              fontWeight: activeView === "split" ? 600 : 400,
              boxShadow: activeView === "split" ? "0 1px 3px rgba(0,0,0,0.08)" : "none",
              cursor: "pointer",
            }}
          >
            🪟 左右对照 (Split)
          </button>
          <button
            type="button"
            onClick={() => handleViewChange("graph")}
            aria-pressed={activeView === "graph"}
            style={{
              padding: "4px 12px",
              fontSize: "12px",
              borderRadius: "4px",
              border: "none",
              backgroundColor: activeView === "graph" ? "var(--paper, #ffffff)" : "transparent",
              color: activeView === "graph" ? "var(--brand-strong, #006d31)" : "var(--muted, #6f7a73)",
              fontWeight: activeView === "graph" ? 600 : 400,
              boxShadow: activeView === "graph" ? "0 1px 3px rgba(0,0,0,0.08)" : "none",
              cursor: "pointer",
            }}
          >
            🕸️ 全景图谱
          </button>
        </div>

        {/* Right Search & Upload Actions */}
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {/* ZG Search */}
          <div style={{ position: "relative", minWidth: 200 }}>
            <input
              type="text"
              aria-label="搜索当前知识库"
              placeholder="ZG 检索 (气隙 / 耐油 / 5V)..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={event => { if (event.key === "Escape") setSearchQuery(""); }}
              style={{
                width: "100%",
                padding: "4px 8px 4px 26px",
                fontSize: "12px",
                backgroundColor: "var(--canvas, #f3f5f2)",
                border: "1px solid var(--line, #d9dfda)",
                borderRadius: "5px",
                color: "var(--ink, #18211c)",
                outline: "none",
              }}
            />
            <span
              style={{
                position: "absolute",
                left: 7,
                top: "50%",
                transform: "translateY(-50%)",
                color: "var(--muted, #6f7a73)",
                display: "flex",
                alignItems: "center",
              }}
            >
              <Icon name="search" size={13} />
            </span>

            {/* Dropdown Snippets */}
            {searchQuery.trim() && (
              <div
                style={{
                  position: "absolute",
                  top: "100%",
                  right: 0,
                  width: 320,
                  backgroundColor: "var(--paper, #ffffff)",
                  border: "1px solid var(--line, #d9dfda)",
                  borderRadius: "6px",
                  marginTop: 4,
                  boxShadow: "0 8px 20px rgba(0,0,0,0.12)",
                  zIndex: 50,
                  maxHeight: 250,
                  overflowY: "auto",
                }}
              >
                {!searchResults.length && <p role="status" style={{ padding: "8px 10px", fontSize: 12 }}>当前知识库没有匹配词条，可修改关键词。</p>}
                {searchResults.map((r) => (
                  <button type="button"
                    key={r.nodeId}
                    aria-label={`打开词条 ${r.title}`}
                    onClick={() => {
                      handleSelectNode(r.nodeId);
                      setSearchQuery("");
                    }}
                    style={{
                      display: "block", width: "100%", textAlign: "left", backgroundColor: "transparent", border: 0,
                      padding: "8px 10px",
                      borderBottom: "1px solid var(--line, #d9dfda)",
                      cursor: "pointer",
                      fontSize: "12px",
                    }}
                    onMouseEnter={(e) =>
                      (e.currentTarget.style.backgroundColor = "var(--canvas, #f3f5f2)")
                    }
                    onMouseLeave={(e) =>
                      (e.currentTarget.style.backgroundColor = "transparent")
                    }
                  >
                    <div style={{ color: "var(--brand-strong, #006d31)", fontWeight: 600 }}>
                      {r.title}
                    </div>
                    <div style={{ color: "var(--ink-soft, #3e4942)", fontSize: "11px", marginTop: 2 }}>
                      {r.snippet}
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Upload Button */}
          <button
            type="button"
            onClick={() => setIsUploadModalOpen(true)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 4,
              padding: "5px 10px",
              fontSize: "12px",
              backgroundColor: "var(--brand, #00893d)",
              border: "none",
              borderRadius: "5px",
              color: "#ffffff",
              cursor: "pointer",
              fontWeight: 500,
            }}
          >
            <Icon name="plus" size={13} />
            <span>添加预览资料</span>
          </button>
        </div>
      </header>

      {/* 2. Three-Pane Body Stage with Resizable Splitters */}
      <div style={{ flex: 1, display: "flex", minHeight: 0, overflow: "hidden" }}>
        {/* Left Column: File Explorer Tree */}
        <KnowledgeFileTree
          selectedNodeId={activePageId}
          onSelectNode={handleSelectNode}
          onOpenUpload={() => setIsUploadModalOpen(true)}
          width={leftWidth}
          nodes={effectiveNodes}
        />

        {/* Left Resizer Handle */}
        <div
          onMouseDown={handleLeftResize}
          title="拖拽改变左侧目录宽度"
          style={{
            width: 4,
            minWidth: 4,
            cursor: "col-resize",
            backgroundColor: "var(--line, #d9dfda)",
            transition: "background-color 0.15s ease",
            zIndex: 10,
          }}
          onMouseEnter={(e) =>
            (e.currentTarget.style.backgroundColor = "var(--brand, #00893d)")
          }
          onMouseLeave={(e) =>
            (e.currentTarget.style.backgroundColor = "var(--line, #d9dfda)")
          }
        />

        {/* Center Main View */}
        <main
          style={{
            flex: 1,
            minWidth: 0,
            height: "100%",
            position: "relative",
            overflow: "hidden",
            display: "flex",
            backgroundColor: "var(--paper, #ffffff)",
          }}
        >
          {activeView === "graph" ? (
            /* Global Full Graph View */
            <div style={{ flex: 1, height: "100%", position: "relative" }}>
              <ForceGraphCanvas
                nodes={effectiveNodes}
                links={effectiveLinks}
                selectedNodeId={activePageId}
                onSelectNode={handleSelectNode}
                highlightedPath={highlightedPath}
              />
            </div>
          ) : (
            /* Document Reader View (Split or Doc) */
            <KnowledgeDocViewer
              nodeId={activePageId}
              onNavigateToNode={handleSelectNode}
              pageContents={effectivePages}
              citationsMap={activeSpace.citations}
            />
          )}
        </main>

        {/* Right Resizer Handle (only when inspector is visible) */}
        {activeView === "split" && (
          <div
            onMouseDown={handleRightResize}
            title="拖拽改变右侧检查栏宽度"
            style={{
              width: 4,
              minWidth: 4,
              cursor: "col-resize",
              backgroundColor: "var(--line, #d9dfda)",
              transition: "background-color 0.15s ease",
              zIndex: 10,
            }}
            onMouseEnter={(e) =>
              (e.currentTarget.style.backgroundColor = "var(--brand, #00893d)")
            }
            onMouseLeave={(e) =>
              (e.currentTarget.style.backgroundColor = "var(--line, #d9dfda)")
            }
          />
        )}

        {/* Right Column: Linked Inspector (in split or doc mode) */}
        {activeView === "split" && (
          <KnowledgeInspector
            currentNodeId={activePageId}
            onNavigateToNode={handleSelectNode}
            width={rightWidth}
            nodes={effectiveNodes}
            links={effectiveLinks}
            complianceCases={activeSpace.complianceCases}
            onTracePath={(path) => {
              setHighlightedPath(path);
              // Switch to graph to show flow
              handleViewChange("graph");
            }}
          />
        )}
      </div>

      {/* Upload Modal */}
      <KnowledgeUploadModal
        isOpen={isUploadModalOpen}
        onClose={() => setIsUploadModalOpen(false)}
        onIngestSuccess={handleIngestSuccess}
      />
    </div>
  );
}
