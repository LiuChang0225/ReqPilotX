// Fresh synthetic demo data; legacy keys only preserve existing route compatibility.
export const KNOWLEDGE_DATA_CLASSIFICATION = "synthetic_fixture" as const;
export interface KnowledgeNode {
  id: string;
  name: string;
  category: "specs" | "ee_hardware" | "software_diagnostics" | "dvp_tests" | "interfaces" | "integration_rules" | "root" | string;
  tags: string[];
  path: string;
  val: number;
  color: string;
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
}

export interface KnowledgeLink {
  source: string;
  target: string;
  value: number;
}

export interface KnowledgeGraphData {
  nodes: KnowledgeNode[];
  links: KnowledgeLink[];
  categories: string[];
  stats: {
    node_count: number;
    link_count: number;
    generated_at: string;
  };
}

export interface ComplianceCase {
  id: string;
  title: string;
  customerText: string;
  matchedNodeId: string;
  verdict: "COMPLIANT" | "DEVIATION" | "CLARIFY";
  baselineValue: string;
  customerValue: string;
  reason: string;
  mappedDvpNodeId: string;
  mappedDvpTest: string;
}

export interface SourceCitation {
  documentName: string;
  category: string;
  section: string;
  evidenceSnippet: string;
}

export interface KnowledgeSpace {
  id: string;
  name: string;
  title: string;
  description: string;
  defaultPageId: string;
  graphData: KnowledgeGraphData;
  pageContents: Record<string, string>;
  citations: Record<string, SourceCitation[]>;
  complianceCases: ComplianceCase[];
}


function demoSpace(id: string, title: string, topic: string): KnowledgeSpace {
  const pageContents = {
    index: "# 合成示例目录\n\n仅演示导航和引用，不代表工程依据。\n\n[[specs/overview]]\n[[dvp_tests/check]]",
    "specs/overview": "# " + topic + "\n\n**合成数据**：示例装置接到启动命令后应点亮指示灯。\n\n验证见 [[dvp_tests/check]]。返回 [[index]]。",
    "dvp_tests/check": "# 启动检查示例\n\n**合成试验**：输入启动命令，观察指示灯。\n\n依据见 [[specs/overview]]，实际项目须另行导入授权依据。",
  };
  const nodes: KnowledgeNode[] = [
    { id: "index", name: "合成示例目录", category: "root", tags: ["合成"], path: "index.md", val: 8, color: "#888888" },
    { id: "specs/overview", name: topic, category: "specs", tags: ["启动", "合成"], path: "specs/overview.md", val: 8, color: "#3b82f6" },
    { id: "dvp_tests/check", name: "启动检查示例", category: "dvp_tests", tags: ["验证"], path: "dvp_tests/check.md", val: 8, color: "#10b981" },
  ];
  const links = [
    { source: "index", target: "specs/overview", value: 1 },
    { source: "index", target: "dvp_tests/check", value: 1 },
    { source: "specs/overview", target: "dvp_tests/check", value: 1 },
  ];
  return {
    id, name: title, title, description: "合成演示，不能用作实际项目的符合性依据。",
    defaultPageId: "specs/overview", pageContents,
    graphData: { nodes, links, categories: ["root", "specs", "dvp_tests"], stats: { node_count: 3, link_count: 3, generated_at: "2026-09-20" } },
    citations: { "specs/overview": [{ documentName: "synthetic-startup-example.md", category: "合成示例", section: "启动", evidenceSnippet: "示例装置接到启动命令后应点亮指示灯。人为编写的演示数据。" }] },
    complianceCases: [{
      id: id + "-demo", title: "合成演示：尚无真实项目依据", customerText: "演示请求：启动后指示灯亮起。",
      matchedNodeId: "specs/overview", verdict: "CLARIFY", baselineValue: "人为编写的演示说明", customerValue: "演示请求",
      reason: "本库不含实际客户文档或已验证产品能力，不能形成真实符合性结论。",
      mappedDvpNodeId: "dvp_tests/check", mappedDvpTest: "合成启动检查",
    }],
  };
}
export const KNOWLEDGE_SPACES: Record<string, KnowledgeSpace> = {
  irps: demoSpace("irps", "领域知识库 · 合成装置示例", "启动指示示例"),
  cus2: demoSpace("cus2", "领域知识库 · 合成控制器示例", "控制器指示示例"),
};
KNOWLEDGE_SPACES.irps_baseline = KNOWLEDGE_SPACES.irps;
KNOWLEDGE_SPACES.cus2_baseline = KNOWLEDGE_SPACES.cus2;
export const IRPS_GRAPH_DATA = KNOWLEDGE_SPACES.irps.graphData;
export const IRPS_PAGE_CONTENTS = KNOWLEDGE_SPACES.irps.pageContents;
export const IRPS_COMPLIANCE_CASES = KNOWLEDGE_SPACES.irps.complianceCases;
export const IRPS_CITATIONS = KNOWLEDGE_SPACES.irps.citations;
export const CUS2_GRAPH_DATA = KNOWLEDGE_SPACES.cus2.graphData;
export const CUS2_PAGE_CONTENTS = KNOWLEDGE_SPACES.cus2.pageContents;
export const CUS2_COMPLIANCE_CASES = KNOWLEDGE_SPACES.cus2.complianceCases;
export const CUS2_CITATIONS = KNOWLEDGE_SPACES.cus2.citations;
export const KNOWLEDGE_GRAPH_DATA = IRPS_GRAPH_DATA;
export const KNOWLEDGE_PAGE_CONTENTS = IRPS_PAGE_CONTENTS;
export const KNOWLEDGE_MARKDOWN_DOCS = IRPS_PAGE_CONTENTS;
export const COMPLIANCE_BENCHMARK_CASES = IRPS_COMPLIANCE_CASES;
export const KNOWLEDGE_COMPLIANCE_CASES = IRPS_COMPLIANCE_CASES;
export const KNOWLEDGE_SOURCE_CITATIONS = IRPS_CITATIONS;
export function searchKnowledgeBase(
  query: string,
  limit = 6,
  activePages: Record<string, string> = IRPS_PAGE_CONTENTS,
  activeNodes: KnowledgeNode[] = IRPS_GRAPH_DATA.nodes
): Array<{
  nodeId: string;
  title: string;
  category: string;
  score: number;
  snippet: string;
}> {
  const trimmed = query.trim().toLowerCase();
  if (!trimmed) return [];

  const tokens = trimmed.split(/\s+/).filter(Boolean);
  const results: Array<{
    nodeId: string;
    title: string;
    category: string;
    score: number;
    snippet: string;
  }> = [];

  for (const node of activeNodes) {
    const content = activePages[node.id] || "";
    const lowerContent = content.toLowerCase();
    const lowerName = node.name.toLowerCase();

    let score = 0;
    let snippet = "";

    // 1. Title exact match
    if (lowerName.includes(trimmed)) {
      score += 10;
    }

    // 2. Token match
    for (const t of tokens) {
      if (lowerName.includes(t)) score += 3;
      if (node.tags.some((tag) => tag.toLowerCase().includes(t))) score += 4;
      if (lowerContent.includes(t)) score += 1;
    }

    // 3. Find matching snippet in lines
    if (score > 0) {
      const lines = content.split("\n");
      for (const line of lines) {
        const lTrim = line.trim();
        if (!lTrim || lTrim.startsWith("---") || lTrim.startsWith("#")) continue;
        const lineLower = lTrim.toLowerCase();
        if (tokens.some((t) => lineLower.includes(t))) {
          snippet = lTrim.replace(/^\|?\s*|\s*\|?$/g, "").slice(0, 100);
          break;
        }
      }

      results.push({
        nodeId: node.id,
        title: node.name,
        category: node.category,
        score,
        snippet: snippet || node.tags.join(", "),
      });
    }
  }

  return results.sort((a, b) => b.score - a.score).slice(0, limit);
}
