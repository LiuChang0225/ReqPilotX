import { useMemo } from "react";
import {
  KNOWLEDGE_GRAPH_DATA,
  COMPLIANCE_BENCHMARK_CASES,
  type KnowledgeNode,
  type KnowledgeLink,
  type ComplianceCase,
} from "./knowledgeData";
import { ForceGraphCanvas, findShortestPath } from "./ForceGraphCanvas";
import { Icon } from "../../shared/ui/Icon";

interface KnowledgeInspectorProps {
  currentNodeId: string;
  onNavigateToNode: (nodeId: string) => void;
  onTracePath: (path: string[]) => void;
  width?: number;
  nodes?: KnowledgeNode[];
  links?: KnowledgeLink[];
  complianceCases?: ComplianceCase[];
}

const referenceVerdicts = {
  COMPLIANT: { label: "符合", foreground: "var(--brand-strong)", background: "var(--brand-soft)" },
  DEVIATION: { label: "有偏离", foreground: "var(--danger)", background: "var(--danger-soft)" },
  CLARIFY: { label: "待澄清", foreground: "var(--warning)", background: "var(--warning-soft)" },
} satisfies Record<ComplianceCase["verdict"], { label: string; foreground: string; background: string }>;

export function KnowledgeInspector({
  currentNodeId,
  onNavigateToNode,
  onTracePath,
  width = 320,
  nodes,
  links,
  complianceCases,
}: KnowledgeInspectorProps) {
  const activeNodes = nodes || KNOWLEDGE_GRAPH_DATA.nodes;
  const activeLinks = links || KNOWLEDGE_GRAPH_DATA.links;
  const activeCases = complianceCases || COMPLIANCE_BENCHMARK_CASES;

  // 1. Calculate Local Graph Sub-network
  const { localNodes, localLinks, incomingNodes, outgoingNodes } = useMemo(() => {
    const connectedNodeIds = new Set<string>([currentNodeId]);
    const incoming: KnowledgeNode[] = [];
    const outgoing: KnowledgeNode[] = [];
    const filteredLinks: KnowledgeLink[] = [];

    const nodeMap = new Map<string, KnowledgeNode>();
    for (const n of activeNodes) nodeMap.set(n.id, n);

    for (const l of activeLinks) {
      if (l.source === currentNodeId) {
        connectedNodeIds.add(l.target);
        filteredLinks.push(l);
        const targetNode = nodeMap.get(l.target);
        if (targetNode && !outgoing.some((n) => n.id === targetNode.id)) {
          outgoing.push(targetNode);
        }
      } else if (l.target === currentNodeId) {
        connectedNodeIds.add(l.source);
        filteredLinks.push(l);
        const sourceNode = nodeMap.get(l.source);
        if (sourceNode && !incoming.some((n) => n.id === sourceNode.id)) {
          incoming.push(sourceNode);
        }
      }
    }

    const filteredNodes = activeNodes.filter((n) =>
      connectedNodeIds.has(n.id)
    );

    return {
      localNodes: filteredNodes,
      localLinks: filteredLinks,
      incomingNodes: incoming,
      outgoingNodes: outgoing,
    };
  }, [currentNodeId, activeNodes, activeLinks]);

  // Reference cases belong to the asset space, not the current project review.
  const matchedComplianceCases = useMemo(() => {
    const knownNodes = new Set(activeNodes.map(node => node.id));
    const knownLinks = activeLinks.filter(link => knownNodes.has(link.source) && knownNodes.has(link.target));
    return activeCases.filter(
      (c) => c.matchedNodeId === currentNodeId || c.mappedDvpNodeId === currentNodeId
    ).map(caseItem => ({
      ...caseItem,
      path: knownNodes.has(caseItem.matchedNodeId) && knownNodes.has(caseItem.mappedDvpNodeId)
        ? findShortestPath(knownLinks, caseItem.matchedNodeId, caseItem.mappedDvpNodeId) : [],
    }));
  }, [currentNodeId, activeCases, activeNodes, activeLinks]);

  return (
    <aside
      data-testid="knowledge-inspector"
      style={{
        width: width,
        minWidth: width,
        maxWidth: width,
        height: "100%",
        display: "flex",
        flexDirection: "column",
        backgroundColor: "var(--paper, #ffffff)",
        borderLeft: "1px solid var(--line, #d9dfda)",
        overflowY: "auto",
        fontSize: "12.5px",
        color: "var(--ink, #18211c)",
      }}
    >
      {/* 1. Local Graph Section */}
      <div style={{ borderBottom: "1px solid var(--line, #d9dfda)" }}>
        <div
          style={{
            padding: "10px 14px",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            backgroundColor: "var(--canvas, #f3f5f2)",
            borderBottom: "1px solid var(--line, #d9dfda)",
            fontWeight: 600,
            fontSize: "12px",
            color: "var(--ink-soft, #3e4942)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ color: "var(--brand, #00893d)" }}>
              <Icon name="spark" size={14} />
            </span>
            <span>局部关系图谱 (Local Graph)</span>
          </div>
          <span style={{ fontSize: "11px", color: "var(--muted, #6f7a73)" }}>
            {localNodes.length} 个直接关联
          </span>
        </div>

        {/* Embedded Local Canvas */}
        <div style={{ width: "100%", height: 210, position: "relative" }}>
          <ForceGraphCanvas
            nodes={localNodes}
            links={localLinks}
            selectedNodeId={currentNodeId}
            onSelectNode={onNavigateToNode}
            isLocalGraph={true}
          />
        </div>
      </div>

      {matchedComplianceCases.length > 0 && (
        <section aria-label="参考案例判定" style={{ padding: "12px 14px", borderBottom: "1px solid var(--line)" }}>
          <h2 style={{ margin: 0, fontSize: 13, fontWeight: 600 }}>参考案例判定 · {matchedComplianceCases.length}</h2>
          <p style={{ margin: "6px 0 10px", color: "var(--muted)", fontSize: 12, lineHeight: 1.5 }}>
            示例用于理解词条的应用，不代表当前项目的评审结论。
          </p>
          {matchedComplianceCases.map((caseItem, index) => {
            const verdict = referenceVerdicts[caseItem.verdict];
            return <details key={`${currentNodeId}:${caseItem.id}`} open={index === 0}
              style={{ paddingBlock: 8, borderTop: "1px solid var(--line)", overflowWrap: "anywhere" }}>
              <summary style={{ cursor: "pointer", fontSize: 12, lineHeight: 1.5 }}>
                <span style={{ fontWeight: 600 }}>{caseItem.title}</span>{" "}
                <span style={{ display: "inline-block", padding: "1px 6px", borderRadius: 8, color: verdict.foreground, backgroundColor: verdict.background, fontWeight: 500 }}>
                  {verdict.label}
                </span>
              </summary>
              <p style={{ margin: "8px 0 4px", color: "var(--muted)", fontSize: 11 }}>案例条款原文</p>
              <blockquote style={{ margin: 0, fontSize: 12, lineHeight: 1.55 }}>{caseItem.customerText}</blockquote>
              <dl style={{ margin: "10px 0", display: "grid", gap: 8, fontSize: 12, lineHeight: 1.5 }}>
                <div><dt style={{ color: "var(--muted)" }}>案例基线</dt><dd style={{ margin: 0 }}>{caseItem.baselineValue}</dd></div>
                <div><dt style={{ color: "var(--muted)" }}>案例要求</dt><dd style={{ margin: 0 }}>{caseItem.customerValue}</dd></div>
                <div><dt style={{ color: "var(--muted)" }}>判定说明</dt><dd style={{ margin: 0 }}>{caseItem.reason}</dd></div>
                <div><dt style={{ color: "var(--muted)" }}>关联测试</dt><dd style={{ margin: 0 }}>{caseItem.mappedDvpTest}</dd></div>
              </dl>
              {caseItem.path.length === 0 && <p style={{ margin: "8px 0", color: "var(--muted)", fontSize: 12 }}>暂无已知词条关联。当前资料未记录连接这两个词条的路径。</p>}
              <button type="button" className="button secondary compact" disabled={caseItem.path.length === 0}
                aria-label={`查看词条关联：${caseItem.title}`}
                title={caseItem.path.length ? "查看资料中已记录的词条链接，不代表因果推导" : "暂无已知词条关联"}
                onClick={() => { if (caseItem.path.length) onTracePath(caseItem.path); }}>
                查看词条关联
              </button>
            </details>;
          })}
        </section>
      )}

      {/* 3. Incoming Backlinks */}
      <div style={{ padding: "12px 14px", borderBottom: "1px solid var(--line, #d9dfda)" }}>
        <div
          style={{
            fontWeight: 600,
            fontSize: "12px",
            color: "var(--ink-soft, #3e4942)",
            marginBottom: 8,
            display: "flex",
            justifyContent: "space-between",
          }}
        >
          <span>🔗 反向链接 (Backlinks)</span>
          <span style={{ color: "var(--muted, #6f7a73)" }}>{incomingNodes.length}</span>
        </div>

        {incomingNodes.length === 0 ? (
          <div style={{ color: "var(--muted, #6f7a73)", fontSize: "11.5px" }}>暂无被引用</div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
            {incomingNodes.map((n) => (
              <button type="button"
                key={n.id}
                onClick={() => onNavigateToNode(n.id)}
                style={{ width: "100%", textAlign: "left",
                  padding: "5px 8px",
                  borderRadius: "4px",
                  backgroundColor: "var(--canvas, #f3f5f2)",
                  cursor: "pointer",
                  fontSize: "11.5px",
                  color: "var(--brand-strong, #006d31)",
                  border: "1px solid var(--line, #d9dfda)",
                  display: "flex",
                  alignItems: "center",
                  gap: 5,
                }}
                title={n.name}
              >
                <Icon name="document" size={12} />
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {n.name.replace(/^iRPS\s*/, "")}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* 4. Outgoing Links */}
      <div style={{ padding: "12px 14px" }}>
        <div
          style={{
            fontWeight: 600,
            fontSize: "12px",
            color: "var(--ink-soft, #3e4942)",
            marginBottom: 8,
            display: "flex",
            justifyContent: "space-between",
          }}
        >
          <span>↗️ 本页引用 (Outgoing Links)</span>
          <span style={{ color: "var(--muted, #6f7a73)" }}>{outgoingNodes.length}</span>
        </div>

        {outgoingNodes.length === 0 ? (
          <div style={{ color: "var(--muted, #6f7a73)", fontSize: "11.5px" }}>暂无外部引用</div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
            {outgoingNodes.map((n) => (
              <button type="button"
                key={n.id}
                onClick={() => onNavigateToNode(n.id)}
                style={{ width: "100%", textAlign: "left",
                  padding: "5px 8px",
                  borderRadius: "4px",
                  backgroundColor: "var(--canvas, #f3f5f2)",
                  cursor: "pointer",
                  fontSize: "11.5px",
                  color: "var(--ink, #18211c)",
                  border: "1px solid var(--line, #d9dfda)",
                  display: "flex",
                  alignItems: "center",
                  gap: 5,
                }}
                title={n.name}
              >
                <Icon name="document" size={12} />
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {n.name.replace(/^iRPS\s*/, "")}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    </aside>
  );
}
