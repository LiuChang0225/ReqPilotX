import { useState } from "react";
import {
  COMPLIANCE_BENCHMARK_CASES,
  KNOWLEDGE_GRAPH_DATA,
  type ComplianceCase,
} from "./knowledgeData";
import { findShortestPath } from "./ForceGraphCanvas";
import { Icon } from "../../shared/ui/Icon";

interface KnowledgeBaselineLensProps {
  selectedCaseId?: string;
  onNavigateToGraphNode?: (nodeId: string) => void;
  onTracePath?: (path: string[]) => void;
}

export function KnowledgeBaselineLens({
  selectedCaseId,
  onNavigateToGraphNode,
  onTracePath,
}: KnowledgeBaselineLensProps) {
  const [activeId, setActiveId] = useState<string>(
    selectedCaseId || COMPLIANCE_BENCHMARK_CASES[0].id
  );

  const activeCase =
    COMPLIANCE_BENCHMARK_CASES.find((c) => c.id === activeId) ||
    COMPLIANCE_BENCHMARK_CASES[0];

  const verdictBadge = (verdict: ComplianceCase["verdict"]) => {
    if (verdict === "COMPLIANT") {
      return (
        <span
          style={{
            padding: "3px 8px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: 600,
            backgroundColor: "rgba(16, 185, 129, 0.15)",
            color: "#34d399",
            border: "1px solid rgba(16, 185, 129, 0.3)",
          }}
        >
          🟢 COMPLIANT (完全合规)
        </span>
      );
    }
    if (verdict === "DEVIATION") {
      return (
        <span
          style={{
            padding: "3px 8px",
            borderRadius: "12px",
            fontSize: "11px",
            fontWeight: 600,
            backgroundColor: "rgba(239, 68, 68, 0.15)",
            color: "#f87171",
            border: "1px solid rgba(239, 68, 68, 0.3)",
          }}
        >
          🔴 DEVIATION (需提技术偏离)
        </span>
      );
    }
    return (
      <span
        style={{
          padding: "3px 8px",
          borderRadius: "12px",
          fontSize: "11px",
          fontWeight: 600,
          backgroundColor: "rgba(245, 158, 11, 0.15)",
          color: "#fbbf24",
          border: "1px solid rgba(245, 158, 11, 0.3)",
        }}
      >
        🟡 CLARIFY (需技术澄清)
      </span>
    );
  };

  const handleTrace = () => {
    const path = findShortestPath(
      KNOWLEDGE_GRAPH_DATA.links,
      activeCase.matchedNodeId,
      activeCase.mappedDvpNodeId
    );
    if (onTracePath) {
      onTracePath(path);
    }
  };

  return (
    <div
      data-testid="knowledge-baseline-lens"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 14,
        padding: "14px 16px",
        backgroundColor: "rgba(15, 23, 42, 0.8)",
        borderRadius: "8px",
        border: "1px solid rgba(148, 163, 184, 0.18)",
        color: "#e2e8f0",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          borderBottom: "1px solid rgba(148, 163, 184, 0.15)",
          paddingBottom: "10px",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ color: "#3b82f6" }}>
            <Icon name="knowledge" size={16} />
          </span>
          <h3 style={{ fontSize: "13px", fontWeight: 600, margin: 0 }}>
            知识基线与合规核对透镜 (Compliance Lens)
          </h3>
        </div>
        {verdictBadge(activeCase.verdict)}
      </div>

      {/* Case Selector Tabs */}
      <div style={{ display: "flex", gap: 6, overflowX: "auto" }}>
        {COMPLIANCE_BENCHMARK_CASES.map((c) => (
          <button
            key={c.id}
            type="button"
            onClick={() => {
              setActiveId(c.id);
              if (onTracePath) onTracePath([]);
            }}
            style={{
              padding: "3px 8px",
              fontSize: "11px",
              borderRadius: "4px",
              backgroundColor:
                activeId === c.id ? "rgba(59, 130, 246, 0.25)" : "transparent",
              color: activeId === c.id ? "#60a5fa" : "#94a3b8",
              border: "1px solid",
              borderColor:
                activeId === c.id
                  ? "rgba(59, 130, 246, 0.4)"
                  : "rgba(148, 163, 184, 0.2)",
              cursor: "pointer",
              whiteSpace: "nowrap",
            }}
          >
            {c.id}
          </button>
        ))}
      </div>

      {/* Customer Requirement Quote */}
      <div
        style={{
          padding: "10px 12px",
          borderRadius: "6px",
          backgroundColor: "rgba(30, 41, 59, 0.5)",
          borderLeft: "3px solid #60a5fa",
          fontSize: "12px",
          lineHeight: 1.5,
        }}
      >
        <div style={{ color: "#94a3b8", fontSize: "11px", marginBottom: 4 }}>
          客户 SOR 原始条款要求：
        </div>
        <div style={{ color: "#f1f5f9" }}>"{activeCase.customerText}"</div>
      </div>

      {/* Comparison Grid */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 10,
          fontSize: "12px",
        }}
      >
        <div
          style={{
            padding: "8px 10px",
            backgroundColor: "rgba(15, 23, 42, 0.6)",
            borderRadius: "6px",
            border: "1px solid rgba(148, 163, 184, 0.12)",
          }}
        >
          <div style={{ color: "#94a3b8", fontSize: "11px" }}>产品标准基线能力</div>
          <div style={{ fontWeight: 600, color: "#38bdf8", marginTop: 2 }}>
            {activeCase.baselineValue}
          </div>
        </div>
        <div
          style={{
            padding: "8px 10px",
            backgroundColor: "rgba(15, 23, 42, 0.6)",
            borderRadius: "6px",
            border: "1px solid rgba(148, 163, 184, 0.12)",
          }}
        >
          <div style={{ color: "#94a3b8", fontSize: "11px" }}>客户申报/要求指标</div>
          <div style={{ fontWeight: 600, color: "#f87171", marginTop: 2 }}>
            {activeCase.customerValue}
          </div>
        </div>
      </div>

      {/* Engineering Reason */}
      <div style={{ fontSize: "12px", lineHeight: 1.5, color: "#cbd5e1" }}>
        <strong style={{ color: "#e2e8f0" }}>判定依据与建议：</strong>{" "}
        {activeCase.reason}
      </div>

      {/* Action Footer with Mapped DVP, Path Trace, & Graph Navigation */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          paddingTop: 8,
          borderTop: "1px solid rgba(148, 163, 184, 0.12)",
          fontSize: "11px",
        }}
      >
        <div style={{ color: "#94a3b8" }}>
          对齐验证用例:{" "}
          <span style={{ color: "#34d399", fontWeight: 500 }}>
            {activeCase.mappedDvpTest}
          </span>
        </div>

        <div style={{ display: "flex", gap: 8 }}>
          <button
            type="button"
            onClick={handleTrace}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 4,
              padding: "4px 8px",
              fontSize: "11px",
              borderRadius: "4px",
              backgroundColor: "rgba(245, 158, 11, 0.15)",
              color: "#fbbf24",
              border: "1px solid rgba(245, 158, 11, 0.3)",
              cursor: "pointer",
            }}
          >
            <span>追踪因果链路</span>
            <Icon name="spark" size={11} />
          </button>

          {onNavigateToGraphNode && (
            <button
              type="button"
              onClick={() => onNavigateToGraphNode(activeCase.matchedNodeId)}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 4,
                padding: "4px 8px",
                fontSize: "11px",
                borderRadius: "4px",
                backgroundColor: "rgba(59, 130, 246, 0.15)",
                color: "#60a5fa",
                border: "1px solid rgba(59, 130, 246, 0.3)",
                cursor: "pointer",
              }}
            >
              <span>在图谱中定位</span>
              <Icon name="external" size={11} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
