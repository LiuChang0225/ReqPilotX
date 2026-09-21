import type {
  EntryClassificationEntry,
  EntryClassificationResponse,
  EntryClassificationSnapshot,
  EntryClassificationType,
  JsonRecord,
  RequirementTreeNode,
  RequirementTreeSnapshot,
} from "../../shared/api/contracts";

const CLASSIFIABLE_PROJECTION_ROLES = new Set([
  "STRUCTURE_PROXY",
  "DIRECT_CONTENT",
  "WHOLE_TABLE_REVIEW",
  "TABLE_RECORD",
  "REQUIREMENT",
]);
const ENTRY_TYPES = new Set<EntryClassificationType>([
  "TECHNICAL",
  "NON_TECHNICAL",
  "INFORMATION",
]);

export type EntryClassificationFilter =
  | "ALL"
  | "UNCLASSIFIED"
  | "HUMAN_OVERRIDE"
  | EntryClassificationType;
export type EntryClassificationBatchScope = "BRANCH" | "FILTERED" | "ALL";

export const ENTRY_CLASSIFICATION_OPTIONS: ReadonlyArray<{
  value: EntryClassificationType;
  label: string;
  shortLabel: string;
  description: string;
}> = [
  {
    value: "TECHNICAL",
    label: "技术需求",
    shortLabel: "技术",
    description: "功能、性能、接口、安全、验证或其他技术约束",
  },
  {
    value: "NON_TECHNICAL",
    label: "非技术需求",
    shortLabel: "非技术",
    description: "项目、商务、交付、组织、服务或流程类约束",
  },
  {
    value: "INFORMATION",
    label: "信息 / 参考",
    shortLabel: "信息参考",
    description: "用于理解上下文但不作为待分析需求的事实或说明",
  },
];

export function effectiveEntryClassificationType(
  entry: EntryClassificationEntry,
): EntryClassificationType | null {
  return entry.effective_type || entry.human_override || entry.suggested_type;
}

export function entryClassificationLabel(
  value: EntryClassificationType | null,
): string {
  return (
    ENTRY_CLASSIFICATION_OPTIONS.find((option) => option.value === value)
      ?.label || "尚未分类"
  );
}

export function entryClassificationForNode(
  classification: EntryClassificationResponse | null,
  nodeId: string,
): EntryClassificationEntry | null {
  return (
    classification?.snapshot.entries.find(
      (entry) => entry.tree_node_id === nodeId,
    ) || null
  );
}

function nodesInClassificationOrder(
  tree: RequirementTreeSnapshot,
): RequirementTreeNode[] {
  const byId = new Map(tree.nodes.map((node) => [node.id, node]));
  const ordered: RequirementTreeNode[] = [];
  const visited = new Set<string>();
  const pending = [...tree.root_node_ids].reverse();
  while (pending.length) {
    const nodeId = pending.pop() || "";
    if (visited.has(nodeId)) continue;
    const node = byId.get(nodeId);
    if (!node) throw new Error("当前需求树包含无法解析的节点关系");
    visited.add(nodeId);
    if (
      node.include_in_view &&
      CLASSIFIABLE_PROJECTION_ROLES.has(String(node.projection_role || ""))
    ) {
      ordered.push(node);
    }
    pending.push(...[...node.child_ids].reverse());
  }
  return ordered;
}

export function assertEntryClassificationMatchesTree(
  response: EntryClassificationResponse,
  tree: RequirementTreeSnapshot,
): EntryClassificationResponse {
  const snapshot = response?.snapshot;
  if (
    response?.schema_version !== "entry-classification-response/v1" ||
    snapshot?.schema_version !== "entry-classification-snapshot/v1" ||
    !["WORKING", "ACCEPTED"].includes(String(snapshot.status)) ||
    !/^[0-9a-f]{64}$/.test(String(snapshot.payload_sha256 || "")) ||
    snapshot.project_id !== tree.source_identity.project_id ||
    snapshot.sor_document_id !== tree.source_identity.document_id ||
    snapshot.source_sha256 !== tree.source_identity.source_sha256 ||
    snapshot.effective_structure_id !==
      tree.structure_identity.effective_snapshot_id
  ) {
    throw new Error("分类结果与当前整理内容不一致，请同步后重试");
  }
  const expected = nodesInClassificationOrder(tree).map((node) => [
    node.id,
    node.workflow_node_id || node.id,
    String(node.projection_role || ""),
  ]);
  const actual = snapshot.entries.map((entry) => [
    entry.tree_node_id,
    entry.workflow_node_id,
    entry.projection_role,
  ]);
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error("分类条目与当前整理内容不一致，请同步后重试");
  }
  const workflowIds = new Set<string>();
  for (const entry of snapshot.entries) {
    if (
      workflowIds.has(entry.workflow_node_id) ||
      (entry.suggested_type !== null && !ENTRY_TYPES.has(entry.suggested_type)) ||
      (entry.human_override !== null && !ENTRY_TYPES.has(entry.human_override)) ||
      (entry.effective_type !== null && !ENTRY_TYPES.has(entry.effective_type))
    ) {
      throw new Error("分类结果无法验证，请同步后重试");
    }
    workflowIds.add(entry.workflow_node_id);
  }
  return response;
}

export function classificationFilterNodeIds(
  tree: RequirementTreeSnapshot,
  classification: EntryClassificationResponse,
  filter: EntryClassificationFilter,
): Set<string> {
  if (filter === "ALL") return new Set(tree.nodes.map((node) => node.id));
  const matching = new Set(
    classification.snapshot.entries
      .filter((entry) => {
        if (filter === "UNCLASSIFIED") {
          return effectiveEntryClassificationType(entry) === null;
        }
        if (filter === "HUMAN_OVERRIDE") {
          return entry.human_override !== null;
        }
        return effectiveEntryClassificationType(entry) === filter;
      })
      .map((entry) => entry.tree_node_id),
  );
  const byId = new Map(tree.nodes.map((node) => [node.id, node]));
  for (const nodeId of [...matching]) {
    let parentId = byId.get(nodeId)?.parent_id || null;
    while (parentId) {
      if (matching.has(parentId)) break;
      matching.add(parentId);
      parentId = byId.get(parentId)?.parent_id || null;
    }
  }
  return matching;
}

function branchNodeIds(
  tree: RequirementTreeSnapshot,
  selectedNodeId: string,
): Set<string> {
  if (!selectedNodeId) return new Set();
  const byId = new Map(tree.nodes.map((node) => [node.id, node]));
  if (!byId.has(selectedNodeId)) return new Set();
  const result = new Set<string>();
  const pending = [selectedNodeId];
  while (pending.length) {
    const nodeId = pending.pop() || "";
    if (result.has(nodeId)) continue;
    result.add(nodeId);
    pending.push(...(byId.get(nodeId)?.child_ids || []));
  }
  return result;
}

export function classificationWorkflowIdsForScope(
  tree: RequirementTreeSnapshot,
  classification: EntryClassificationResponse,
  options: {
    scope: EntryClassificationBatchScope;
    selectedNodeId: string;
    visibleNodeIds: Set<string>;
  },
): string[] {
  const targetNodeIds =
    options.scope === "ALL"
      ? null
      : options.scope === "FILTERED"
        ? options.visibleNodeIds
        : branchNodeIds(tree, options.selectedNodeId);
  return classification.snapshot.entries
    .filter(
      (entry) =>
        targetNodeIds === null || targetNodeIds.has(entry.tree_node_id),
    )
    .map((entry) => entry.workflow_node_id);
}

export function entryClassificationCommandPayload(
  snapshot: EntryClassificationSnapshot,
  commandId: string,
) {
  return {
    command_id: commandId,
    expected_revision: snapshot.revision,
    expected_snapshot_sha256: snapshot.payload_sha256,
    expected_source_sha256: snapshot.source_sha256,
    expected_working_tree_sha256: snapshot.working_tree_sha256,
    expected_effective_structure_id: snapshot.effective_structure_id,
    expected_policy_version: snapshot.policy_version,
  };
}

export function assertPublicationMatchesAcceptedClassification(
  published: JsonRecord,
  classification: EntryClassificationResponse,
): void {
  const publication =
    published.publication && typeof published.publication === "object"
      ? (published.publication as JsonRecord)
      : {};
  if (
    classification.snapshot.status !== "ACCEPTED" ||
    String(publication.entry_classification_snapshot_id || "") !==
      classification.snapshot.snapshot_id ||
    String(publication.entry_classification_snapshot_sha256 || "") !==
      classification.snapshot.payload_sha256
  ) {
    throw new Error("生成结果未绑定刚接受的清单分类，系统已停止激活该版本");
  }
}

export function assertPublicationReadyForActivation(
  published: JsonRecord,
  classification: EntryClassificationResponse,
): void {
  assertPublicationMatchesAcceptedClassification(published, classification);
  const publication =
    published.publication && typeof published.publication === "object"
      ? (published.publication as JsonRecord)
      : {};
  const publicationState = String(
    publication.publication_state || "",
  ).toUpperCase();
  if (publicationState === "PUBLISHING") {
    throw new Error(
      "确认清单版本仍在生成中，请稍后重试；系统尚未激活该版本",
    );
  }
  if (publicationState !== "READY") {
    throw new Error("确认清单发布状态尚未就绪，系统尚未激活该版本");
  }
  if (publication.requirement_tree_persisted !== true) {
    throw new Error("确认清单的可信需求树尚未持久化，系统尚未激活该版本");
  }
}

export function createEntryClassificationCommandId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `entry-classification-${Date.now()}-${Math.random()
    .toString(16)
    .slice(2)}`;
}
