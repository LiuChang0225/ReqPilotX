import { useMemo } from "react";
import {
  KNOWLEDGE_PAGE_CONTENTS,
  KNOWLEDGE_SOURCE_CITATIONS,
  type SourceCitation,
} from "./knowledgeData";
import { Icon } from "../../shared/ui/Icon";
import { renderKnowledgeInline, splitKnowledgeTableRow } from "./knowledgeInline";

interface KnowledgeDocViewerProps {
  nodeId: string;
  onNavigateToNode: (targetNodeId: string) => void;
  pageContents?: Record<string, string>;
  citationsMap?: Record<string, SourceCitation[]>;
}

export function KnowledgeDocViewer({
  nodeId,
  onNavigateToNode,
  pageContents,
  citationsMap,
}: KnowledgeDocViewerProps) {
  const activePages = pageContents || KNOWLEDGE_PAGE_CONTENTS;
  const activeCitations = citationsMap || KNOWLEDGE_SOURCE_CITATIONS;

  const content = activePages[nodeId] || "";
  const citations = activeCitations[nodeId] || [];

  const { metadata, body } = useMemo(() => {
    if (!content) return { metadata: null, body: "" };

    const fmMatch = content.match(/^---\n([\s\S]*?)\n---/);
    if (!fmMatch) return { metadata: null, body: content };

    const rawFm = fmMatch[1];
    const restBody = content.slice(fmMatch[0].length).trim();

    const titleMatch = rawFm.match(/^title:\s*(.*)$/m);
    const categoryMatch = rawFm.match(/^category:\s*(.*)$/m);
    const tagsMatch = rawFm.match(/^tags:\s*\[(.*?)\]/m);
    const updatedMatch = rawFm.match(/^last_updated:\s*(.*)$/m);

    return {
      metadata: {
        title: titleMatch ? titleMatch[1].trim() : nodeId,
        category: categoryMatch ? categoryMatch[1].trim() : "specs",
        tags: tagsMatch
          ? tagsMatch[1]
              .split(",")
              .map((t) => t.trim())
              .filter(Boolean)
          : [],
        lastUpdated: updatedMatch ? updatedMatch[1].trim() : "",
      },
      body: restBody,
    };
  }, [content, nodeId]);

  const renderInlineText = (text: string) => renderKnowledgeInline(text, (cleanTarget, displayText, key) => (
        <button
          key={key}
          type="button"
          onClick={() => onNavigateToNode(cleanTarget)}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 3,
            padding: "2px 7px",
            fontSize: "12px",
            borderRadius: "4px",
            backgroundColor: "var(--brand-faint, #f1f8f3)",
            color: "var(--brand-strong, #006d31)",
            border: "1px solid var(--line, #d9dfda)",
            cursor: "pointer",
            fontWeight: 500,
            margin: "0 2px",
            verticalAlign: "baseline",
          }}
          title={`穿透跳转至词条: ${cleanTarget}`}
        >
          <span style={{ opacity: 0.7 }}>[[</span>
          <span>{displayText}</span>
          <span style={{ opacity: 0.7 }}>]]</span>
        </button>
  ));

  // Render markdown sections, headers and tables
  const renderMarkdownSection = (text: string) => {
    const lines = text.split("\n");
    const elements: React.ReactNode[] = [];
    let inTable = false;
    let tableRows: string[][] = [];

    const flushTable = (keyIndex: number) => {
      if (tableRows.length === 0) return;
      const headers = tableRows[0];
      const dataRows = tableRows.slice(2);
      elements.push(
        <div
          key={`table-${keyIndex}`}
          style={{
            overflowX: "auto",
            margin: "14px 0",
            borderRadius: "6px",
            border: "1px solid var(--line, #d9dfda)",
            boxShadow: "0 1px 3px rgba(0,0,0,0.02)",
          }}
        >
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: "12.5px",
              textAlign: "left",
              backgroundColor: "var(--paper, #ffffff)",
            }}
          >
            <thead>
              <tr style={{ backgroundColor: "var(--canvas, #f3f5f2)" }}>
                {headers.map((h, i) => (
                  <th
                    key={i}
                    style={{
                      padding: "9px 12px",
                      borderBottom: "1px solid var(--line-strong, #bfc9c2)",
                      color: "var(--ink-soft, #3e4942)",
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
                    borderBottom: "1px solid var(--line, #d9dfda)",
                    backgroundColor:
                      rIdx % 2 === 0 ? "transparent" : "var(--canvas, #f3f5f2)",
                  }}
                >
                  {row.map((cell, cIdx) => (
                    <td
                      key={cIdx}
                      style={{
                        padding: "8px 12px",
                        color: "var(--ink, #18211c)",
                        lineHeight: 1.45,
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

      if (trimmed.startsWith("|") && trimmed.endsWith("|")) {
        inTable = true;
        const cols = splitKnowledgeTableRow(trimmed);
        tableRows.push(cols);
        return;
      } else if (inTable) {
        flushTable(idx);
      }

      if (trimmed.startsWith("# ")) {
        elements.push(
          <h1
            key={idx}
            style={{
              fontSize: "20px",
              fontWeight: 700,
              color: "var(--ink, #18211c)",
              margin: "20px 0 12px 0",
              borderBottom: "1px solid var(--line, #d9dfda)",
              paddingBottom: "8px",
              display: "flex",
              alignItems: "center",
              gap: 8,
            }}
          >
            <span
              style={{
                width: 4,
                height: 18,
                backgroundColor: "var(--brand, #00893d)",
                borderRadius: 2,
              }}
            />
            <span>{renderInlineText(trimmed.slice(2))}</span>
          </h1>
        );
      } else if (trimmed.startsWith("## ")) {
        elements.push(
          <h2
            key={idx}
            style={{
              fontSize: "16px",
              fontWeight: 600,
              color: "var(--ink-soft, #3e4942)",
              margin: "18px 0 10px 0",
              borderBottom: "1px dashed var(--line, #d9dfda)",
              paddingBottom: "4px",
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
              color: "var(--brand-strong, #006d31)",
              margin: "14px 0 6px 0",
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
              color: "var(--ink, #18211c)",
              marginLeft: "18px",
              marginBottom: "5px",
              lineHeight: 1.6,
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
              color: "var(--ink, #18211c)",
              margin: "8px 0",
              lineHeight: 1.65,
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

  return (
    <article
      data-testid="knowledge-doc-viewer"
      style={{
        flex: 1,
        height: "100%",
        overflowY: "auto",
        backgroundColor: "var(--paper, #ffffff)",
        padding: "24px 36px",
        color: "var(--ink, #18211c)",
      }}
    >
      {/* Breadcrumb Header */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 16,
          paddingBottom: 10,
          borderBottom: "1px solid var(--line, #d9dfda)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "12px" }}>
          <span style={{ color: "var(--muted, #6f7a73)" }}>知识库</span>
          <span style={{ color: "var(--line-strong, #bfc9c2)" }}>/</span>
          <span
            style={{
              padding: "2px 8px",
              borderRadius: "4px",
              backgroundColor: "var(--canvas, #f3f5f2)",
              color: "var(--ink-soft, #3e4942)",
              fontWeight: 500,
            }}
          >
            {metadata?.category}
          </span>
          <span style={{ color: "var(--line-strong, #bfc9c2)" }}>/</span>
          <span style={{ fontWeight: 600, color: "var(--ink, #18211c)" }}>
            {metadata?.title}
          </span>
        </div>
        {metadata?.lastUpdated && (
          <span style={{ fontSize: "11px", color: "var(--muted, #6f7a73)" }}>
            核对时间: {metadata.lastUpdated}
          </span>
        )}
      </div>

      {/* Citations / Source Document Proof Cards */}
      {citations.length > 0 && (
        <div
          data-testid="source-citation-card"
          style={{
            marginBottom: 20,
            padding: "12px 14px",
            backgroundColor: "var(--brand-faint, #f1f8f3)",
            border: "1px solid var(--line, #d9dfda)",
            borderRadius: "6px",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              fontSize: "12px",
              fontWeight: 600,
              color: "var(--brand-strong, #006d31)",
              marginBottom: 8,
            }}
          >
            <Icon name="document" size={14} />
            <span>原始工程技术资料依据 (Source Evidence)</span>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {citations.map((c, idx) => (
              <div
                key={idx}
                style={{
                  padding: "8px 10px",
                  backgroundColor: "var(--paper, #ffffff)",
                  borderRadius: "4px",
                  border: "1px solid var(--line, #d9dfda)",
                  fontSize: "12px",
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
                  <span style={{ fontWeight: 600, color: "var(--ink, #18211c)" }}>
                    📄 {c.documentName}
                  </span>
                  <span
                    style={{
                      fontSize: "10px",
                      padding: "1px 6px",
                      borderRadius: "10px",
                      backgroundColor: "var(--canvas, #f3f5f2)",
                      color: "var(--muted, #6f7a73)",
                    }}
                  >
                    {c.category}
                  </span>
                </div>
                <div style={{ color: "var(--brand-strong, #006d31)", fontSize: "11px", marginBottom: 4 }}>
                  章节位置: {c.section}
                </div>
                <div style={{ color: "var(--ink-soft, #3e4942)", fontSize: "11.5px", lineHeight: 1.45 }}>
                  "{c.evidenceSnippet}"
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Metadata Tags */}
      {metadata && metadata.tags.length > 0 && (
        <div style={{ display: "flex", gap: 6, marginBottom: 16 }}>
          {metadata.tags.map((tag) => (
            <span
              key={tag}
              style={{
                fontSize: "11px",
                padding: "2px 8px",
                borderRadius: "12px",
                backgroundColor: "var(--canvas, #f3f5f2)",
                color: "var(--muted, #6f7a73)",
                border: "1px solid var(--line, #d9dfda)",
              }}
            >
              #{tag}
            </span>
          ))}
        </div>
      )}

      {/* Main Markdown Body */}
      <div>{renderMarkdownSection(body)}</div>
    </article>
  );
}
