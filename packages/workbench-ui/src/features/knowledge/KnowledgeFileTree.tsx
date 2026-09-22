import { useState, useMemo } from "react";
import { KNOWLEDGE_GRAPH_DATA, type KnowledgeNode } from "./knowledgeData";
import { Icon } from "../../shared/ui/Icon";

interface KnowledgeFileTreeProps {
  selectedNodeId: string;
  onSelectNode: (nodeId: string) => void;
  onOpenUpload: () => void;
  width?: number;
  nodes?: KnowledgeNode[];
}

interface CategoryGroup {
  key: string;
  label: string;
  iconName: "folder" | "tree";
  color: string;
  nodes: KnowledgeNode[];
}

const CATEGORY_DEFINITIONS: Array<{
  key: string;
  label: string;
  color: string;
}> = [
  { key: "specs", label: "产品规格包线 (Specs)", color: "#2563eb" },
  { key: "ee_hardware", label: "电子电气硬件 (E/E Hardware)", color: "#8b5cf6" },
  { key: "software_diagnostics", label: "软件通信诊断 (Software)", color: "#06b6d4" },
  { key: "dvp_tests", label: "DVP测试用例 (Tests)", color: "#059669" },
  { key: "interfaces", label: "接口与连接器 (Interfaces)", color: "#d97706" },
  { key: "integration_rules", label: "系统集成准则 (Rules)", color: "#db2777" },
  { key: "root", label: "总纲大纲 (Index)", color: "#64748b" },
];

export function KnowledgeFileTree({
  selectedNodeId,
  onSelectNode,
  onOpenUpload,
  width = 250,
  nodes: propNodes,
}: KnowledgeFileTreeProps) {
  const [searchFilter, setSearchFilter] = useState("");
  const [collapsedCategories, setCollapsedCategories] = useState<Record<string, boolean>>({});

  const toggleCategory = (catKey: string) => {
    setCollapsedCategories((prev) => ({
      ...prev,
      [catKey]: !prev[catKey],
    }));
  };

  const activeNodes = propNodes || KNOWLEDGE_GRAPH_DATA.nodes;

  const groups = useMemo<CategoryGroup[]>(() => {
    const filter = searchFilter.trim().toLowerCase();
    const map = new Map<string, KnowledgeNode[]>();

    for (const def of CATEGORY_DEFINITIONS) {
      map.set(def.key, []);
    }

    for (const node of activeNodes) {
      const matchSearch =
        !filter ||
        node.name.toLowerCase().includes(filter) ||
        node.id.toLowerCase().includes(filter) ||
        node.tags.some((t) => t.toLowerCase().includes(filter));

      if (matchSearch) {
        const cat = node.category || "root";
        if (!map.has(cat)) map.set(cat, []);
        map.get(cat)!.push(node);
      }
    }

    return CATEGORY_DEFINITIONS.map((def) => ({
      key: def.key,
      label: def.label,
      iconName: "folder" as const,
      color: def.color,
      nodes: map.get(def.key) || [],
    })).filter((g) => g.nodes.length > 0 || !filter);
  }, [searchFilter, activeNodes]);

  return (
    <nav
      data-testid="knowledge-file-tree"
      style={{
        width: width,
        minWidth: width,
        maxWidth: width,
        height: "100%",
        display: "flex",
        flexDirection: "column",
        backgroundColor: "var(--paper, #ffffff)",
        borderRight: "1px solid var(--line, #d9dfda)",
        overflow: "hidden",
        fontSize: "13px",
      }}
    >
      {/* Header with Title & Action */}
      <div
        style={{
          padding: "12px 14px 8px 14px",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          borderBottom: "1px solid var(--line, #d9dfda)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ color: "var(--brand, #00893d)" }}>
            <Icon name="tree" size={16} />
          </span>
          <span style={{ fontWeight: 600, color: "var(--ink, #18211c)" }}>
            知识库文档树
          </span>
        </div>
        <button
          type="button"
          onClick={onOpenUpload}
          title="添加本次预览资料" aria-label="添加本次预览资料"
          style={{
            background: "none",
            border: "none",
            color: "var(--muted, #6f7a73)",
            cursor: "pointer",
            padding: 4,
            borderRadius: "4px",
            display: "flex",
            alignItems: "center",
          }}
        >
          <Icon name="plus" size={15} />
        </button>
      </div>

      {/* Filter Input */}
      <div style={{ padding: "8px 12px" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            padding: "4px 8px",
            backgroundColor: "var(--canvas, #f3f5f2)",
            borderRadius: "6px",
            border: "1px solid var(--line, #d9dfda)",
          }}
        >
          <span style={{ color: "var(--muted, #6f7a73)" }}>
            <Icon name="search" size={13} />
          </span>
          <input
            type="text"
            placeholder="过滤词条或标签..." aria-label="过滤知识词条"
            value={searchFilter}
            onChange={(e) => setSearchFilter(e.target.value)}
            style={{
              border: "none",
              background: "transparent",
              outline: "none",
              fontSize: "12px",
              width: "100%",
              color: "var(--ink, #18211c)",
            }}
          />
        </div>
      </div>

      {/* Tree Content */}
      <div style={{ flex: 1, overflowY: "auto", padding: "4px 6px" }}>
        {groups.length === 0 && <p role="status" style={{ padding: 8, fontSize: 12 }}>没有匹配词条，请修改过滤条件。</p>}
        {groups.map((group) => {
          const isCollapsed = collapsedCategories[group.key];
          return (
            <div key={group.key} style={{ marginBottom: 4 }}>
              {/* Category Header */}
              <button type="button" aria-expanded={!isCollapsed}
                onClick={() => toggleCategory(group.key)}
                style={{ width: "100%", border: 0, background: "transparent", textAlign: "left",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  padding: "5px 8px",
                  borderRadius: "4px",
                  cursor: "pointer",
                  color: "var(--ink-soft, #3e4942)",
                  fontWeight: 600,
                  fontSize: "12px",
                  userSelect: "none",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
                  <span style={{ color: "var(--muted, #6f7a73)" }}>
                    <Icon
                      name={isCollapsed ? "chevron" : "chevron-down"}
                      size={12}
                    />
                  </span>
                  <span
                    style={{
                      width: 7,
                      height: 7,
                      borderRadius: "50%",
                      backgroundColor: group.color,
                      display: "inline-block",
                    }}
                  />
                  <span>{group.label}</span>
                </div>
                <span
                  style={{
                    fontSize: "10px",
                    padding: "1px 5px",
                    borderRadius: "10px",
                    backgroundColor: "var(--canvas-deep, #e9ede9)",
                    color: "var(--muted, #6f7a73)",
                  }}
                >
                  {group.nodes.length}
                </span>
              </button>

              {/* Category Children */}
              {!isCollapsed && (
                <div style={{ marginLeft: 16, borderLeft: "1px solid var(--line, #d9dfda)" }}>
                  {group.nodes.map((node) => {
                    const isSelected = node.id === selectedNodeId;
                    return (
                      <button type="button" aria-current={isSelected ? "page" : undefined}
                        key={node.id}
                        onClick={() => onSelectNode(node.id)}
                        style={{ width: "100%", border: 0, textAlign: "left",
                          display: "flex",
                          alignItems: "center",
                          gap: 6,
                          padding: "5px 8px 5px 12px",
                          margin: "1px 0",
                          borderRadius: "4px",
                          cursor: "pointer",
                          backgroundColor: isSelected
                            ? "var(--brand-soft, #e5f3ea)"
                            : "transparent",
                          color: isSelected
                            ? "var(--brand-strong, #006d31)"
                            : "var(--ink-soft, #3e4942)",
                          fontWeight: isSelected ? 600 : 400,
                          fontSize: "12px",
                          whiteSpace: "nowrap",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          transition: "all 0.1s ease",
                        }}
                        title={node.name}
                      >
                        <span
                          style={{
                            color: isSelected
                              ? "var(--brand-strong, #006d31)"
                              : "var(--muted, #6f7a73)",
                            display: "flex",
                          }}
                        >
                          <Icon name="document" size={13} />
                        </span>
                        <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                          {node.name.replace(/^iRPS\s*/, "")}
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Footer Info */}
      <div
        style={{
          padding: "8px 12px",
          borderTop: "1px solid var(--line, #d9dfda)",
          fontSize: "11px",
          color: "var(--muted, #6f7a73)",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          backgroundColor: "var(--canvas, #f3f5f2)",
        }}
      >
        <span>当前 {activeNodes.length} 篇词条</span>
      </div>
    </nav>
  );
}
