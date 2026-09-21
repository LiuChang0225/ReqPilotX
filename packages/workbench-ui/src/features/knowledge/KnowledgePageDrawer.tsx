import { useMemo } from "react";
import { KNOWLEDGE_PAGE_CONTENTS } from "./knowledgeData";
import { Icon } from "../../shared/ui/Icon";
import { renderKnowledgeInline, splitKnowledgeTableRow } from "./knowledgeInline";

interface KnowledgePageDrawerProps {
  nodeId: string | null;
  onClose: () => void;
  onNavigateToNode: (targetNodeId: string) => void;
}

export function KnowledgePageDrawer({
  nodeId,
  onClose,
  onNavigateToNode,
}: KnowledgePageDrawerProps) {
  const content = nodeId ? KNOWLEDGE_PAGE_CONTENTS[nodeId] : null;

  // Parse Frontmatter and markdown body
  const { metadata, body } = useMemo(() => {
    if (!content) return { metadata: null, body: "" };

    const fmMatch = content.match(/^---\n([\s\S]*?)\n---/);
    if (!fmMatch) return { metadata: null, body: content };

    const rawFm = fmMatch[1];
    const restBody = content.slice(fmMatch[0].length).trim();

    const titleMatch = rawFm.match(/^title:\s*(.*)$/m);
    const categoryMatch = rawFm.match(/^category:\s*(.*)$/m);
    const tagsMatch = rawFm.match(/^tags:\s*\[(.*?)\]/m);
    const aliasesMatch = rawFm.match(/^aliases:\s*\[(.*?)\]/m);
    const updatedMatch = rawFm.match(/^last_updated:\s*(.*)$/m);

    return {
      metadata: {
        title: titleMatch ? titleMatch[1].trim() : nodeId,
        category: categoryMatch ? categoryMatch[1].trim() : "general",
        tags: tagsMatch
          ? tagsMatch[1]
              .split(",")
              .map((t) => t.trim())
              .filter(Boolean)
          : [],
        aliases: aliasesMatch
          ? aliasesMatch[1]
              .split(",")
              .map((a) => a.trim())
              .filter(Boolean)
          : [],
        lastUpdated: updatedMatch ? updatedMatch[1].trim() : "",
      },
      body: restBody,
    };
  }, [content, nodeId]);

  if (!nodeId || !content) return null;

  // Render markdown with wikilink detection and basic table styling
  const renderMarkdownSection = (text: string) => {
    const lines = text.split("\n");
    const elements: React.ReactNode[] = [];
    let inTable = false;
    let tableRows: string[][] = [];

    const flushTable = (keyIndex: number) => {
      if (tableRows.length === 0) return;
      const headers = tableRows[0];
      const dataRows = tableRows.slice(2); // skip separator row
      elements.push(
        <div
          key={`table-${keyIndex}`}
          style={{
            overflowX: "auto",
            margin: "12px 0",
            borderRadius: "6px",
            border: "1px solid rgba(148, 163, 184, 0.2)",
          }}
        >
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: "13px",
              textAlign: "left",
            }}
          >
            <thead>
              <tr style={{ backgroundColor: "rgba(30, 41, 59, 0.6)" }}>
                {headers.map((h, i) => (
                  <th
                    key={i}
                    style={{
                      padding: "8px 12px",
                      borderBottom: "1px solid rgba(148, 163, 184, 0.2)",
                      color: "#94a3b8",
                      fontWeight: 600,
                    }}
                  >
                    {renderInlineText(h.trim())}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {dataRows.map((row, rIdx) => (
                <tr
                  key={rIdx}
                  style={{
                    borderBottom: "1px solid rgba(148, 163, 184, 0.1)",
                    backgroundColor:
                      rIdx % 2 === 0 ? "transparent" : "rgba(255, 255, 255, 0.02)",
                  }}
                >
                  {row.map((cell, cIdx) => (
                    <td
                      key={cIdx}
                      style={{
                        padding: "8px 12px",
                        color: "#e2e8f0",
                      }}
                    >
                      {renderInlineText(cell.trim())}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
      tableRows = [];
      inTable = false;
    };

    lines.forEach((line, idx) => {
      const trimmed = line.trim();

      // Check table row
      if (trimmed.startsWith("|") && trimmed.endsWith("|")) {
        inTable = true;
        const cols = splitKnowledgeTableRow(trimmed);
        tableRows.push(cols);
        return;
      } else if (inTable) {
        flushTable(idx);
      }

      // Headings
      if (trimmed.startsWith("# ")) {
        elements.push(
          <h1
            key={idx}
            style={{
              fontSize: "20px",
              fontWeight: 700,
              color: "#f8fafc",
              margin: "18px 0 10px 0",
              borderBottom: "1px solid rgba(148, 163, 184, 0.2)",
              paddingBottom: "6px",
            }}
          >
            {renderInlineText(trimmed.slice(2))}
          </h1>
        );
      } else if (trimmed.startsWith("## ")) {
        elements.push(
          <h2
            key={idx}
            style={{
              fontSize: "16px",
              fontWeight: 600,
              color: "#60a5fa",
              margin: "16px 0 8px 0",
            }}
          >
            {renderInlineText(trimmed.slice(3))}
          </h2>
        );
      } else if (trimmed.startsWith("### ")) {
        elements.push(
          <h3
            key={idx}
            style={{
              fontSize: "14px",
              fontWeight: 600,
              color: "#cbd5e1",
              margin: "12px 0 6px 0",
            }}
          >
            {renderInlineText(trimmed.slice(4))}
          </h3>
        );
      } else if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
        elements.push(
          <li
            key={idx}
            style={{
              fontSize: "13px",
              color: "#cbd5e1",
              marginLeft: "18px",
              marginBottom: "4px",
              lineHeight: 1.5,
            }}
          >
            {renderInlineText(trimmed.slice(2))}
          </li>
        );
      } else if (trimmed === "") {
        // empty space
      } else {
        elements.push(
          <p
            key={idx}
            style={{
              fontSize: "13px",
              color: "#cbd5e1",
              margin: "6px 0",
              lineHeight: 1.6,
            }}
          >
            {renderInlineText(trimmed)}
          </p>
        );
      }
    });

    if (inTable) flushTable(lines.length);
    return elements;
  };

  const renderInlineText = (text: string) => renderKnowledgeInline(text, (cleanTarget, displayText, key) => (
        <button
          key={key}
          type="button"
          onClick={() => onNavigateToNode(cleanTarget)}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 2,
            padding: "1px 6px",
            fontSize: "12px",
            borderRadius: "4px",
            backgroundColor: "rgba(59, 130, 246, 0.15)",
            color: "#60a5fa",
            border: "1px solid rgba(59, 130, 246, 0.3)",
            cursor: "pointer",
            fontWeight: 500,
            textDecoration: "none",
            margin: "0 2px",
          }}
          title={`跳转到: ${cleanTarget}`}
        >
          {displayText}
        </button>
  ));

  return (
    <aside
      data-testid="knowledge-page-drawer"
      style={{
        width: 480,
        height: "100%",
        backgroundColor: "#0b1329",
        borderLeft: "1px solid rgba(148, 163, 184, 0.18)",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        zIndex: 20,
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "14px 18px",
          borderBottom: "1px solid rgba(148, 163, 184, 0.15)",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          backgroundColor: "rgba(15, 23, 42, 0.8)",
        }}
      >
        <div>
          <span
            style={{
              fontSize: "11px",
              padding: "2px 8px",
              borderRadius: "12px",
              backgroundColor: "rgba(59, 130, 246, 0.2)",
              color: "#93c5fd",
              textTransform: "uppercase",
              fontWeight: 600,
              marginRight: 8,
            }}
          >
            {metadata?.category || "Node"}
          </span>
          <span style={{ fontSize: "11px", color: "#64748b" }}>{nodeId}</span>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭详情"
          style={{
            background: "none",
            border: "none",
            color: "#94a3b8",
            cursor: "pointer",
            padding: 4,
            display: "flex",
            alignItems: "center",
          }}
        >
          <Icon name="close" size={16} />
        </button>
      </div>

      {/* Metadata Pills */}
      {metadata && (
        <div
          style={{
            padding: "10px 18px",
            backgroundColor: "rgba(15, 23, 42, 0.4)",
            borderBottom: "1px solid rgba(148, 163, 184, 0.1)",
            display: "flex",
            flexWrap: "wrap",
            gap: 6,
          }}
        >
          {metadata.tags.map((tag) => (
            <span
              key={tag}
              style={{
                fontSize: "11px",
                color: "#94a3b8",
                backgroundColor: "rgba(30, 41, 59, 0.8)",
                padding: "2px 6px",
                borderRadius: "4px",
              }}
            >
              #{tag}
            </span>
          ))}
        </div>
      )}

      {/* Content Scroll Area */}
      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "16px 20px",
        }}
      >
        {renderMarkdownSection(body)}
      </div>
    </aside>
  );
}
