import type {
  EntryClassificationEntry,
  EntryClassificationResponse,
  EntryClassificationType,
  RequirementAtomizationScopeAttempt,
  RequirementTreeNode,
  RequirementTreeSnapshot,
} from "../../shared/api/contracts";

export type AtomizationScope = "CURRENT" | "SELECTED" | "BRANCH" | "ALL";
export type AtomizationScopeOperation = "APPLY" | "UNDO";
export type AtomizationUnselectedPolicy = "SOURCE_BOUND_PASSTHROUGH";
export type AtomizationScopeSkipReason =
  | "STRUCTURE"
  | "UNCLASSIFIED"
  | "NON_TECHNICAL"
  | "INFORMATION"
  | "NOT_DIRECT_OR_NOT_REQUIREMENT";

export interface AtomizationScopeIdentity {
  projectId: string;
  documentId: string;
  sourceSha256: string;
  effectiveStructureId: string;
  effectiveTreeSha256: string;
  workingTreeId: string;
  workingTreeSha256: string;
}

export interface AtomizationScopeItem {
  treeNodeId: string;
  workflowNodeId: string | null;
  label: string;
  pathLabel: string;
  sourceOrder: number;
  sourceOffset: number;
  classificationType: EntryClassificationType | null;
  disposition: "TARGET" | "SKIPPED";
  reason: AtomizationScopeSkipReason | null;
}

export interface AtomizationScopePlan {
  identity: AtomizationScopeIdentity;
  scope: AtomizationScope;
  anchorTreeNodeId: string | null;
  selectedWorkflowIds: string[];
  items: AtomizationScopeItem[];
  requestedTreeNodeIds: string[];
  requestedWorkflowIds: string[];
  executableWorkflowIds: string[];
  estimatedRemoteCallCount: number;
  targets: AtomizationScopeItem[];
  skipped: AtomizationScopeItem[];
}

export interface AtomizationScopeViewItem extends AtomizationScopeItem {
  reasonLabel: string;
}

export interface AtomizationScopeViewModel {
  plan: AtomizationScopePlan;
  scope: AtomizationScope;
  scopeLabel: string;
  defaultBehavior: string;
  summary: string;
  actionLabel: string;
  targetCount: number;
  skippedCount: number;
  estimatedRemoteCallCount: number;
  canApply: boolean;
  emptyReason: string;
  targets: AtomizationScopeViewItem[];
  skipped: AtomizationScopeViewItem[];
}

export interface AtomizationAttemptActionGate {
  serverRunning: boolean;
  openScopeBlocked: boolean;
  applyBlocked: boolean;
  undoBlocked: boolean;
  retryingApply: boolean;
  guidance: string;
}

export interface AtomizationPreviewFingerprintPayload {
  identity: AtomizationScopeIdentity;
  scope: AtomizationScope;
  anchorTreeNodeId: string | null;
  selectedWorkflowIds: string[];
  requestedTreeNodeIds: string[];
  requestedWorkflowIds: string[];
  executableWorkflowIds: string[];
  skipped: Array<{
    treeNodeId: string;
    workflowNodeId: string | null;
    reason: AtomizationScopeSkipReason;
  }>;
}

export interface AtomizationScopeWireHashInput {
  project_id: string;
  document_id: string;
  session_id: string;
  command_id: string;
  operation: AtomizationScopeOperation;
  scope: AtomizationScope;
  expected_review_revision: number;
  expected_scope_revision: number;
  expected_entry_classification_snapshot_id: string;
  expected_entry_classification_revision: number;
  expected_entry_classification_sha256: string;
  expected_working_tree_id: string;
  expected_working_tree_sha256: string;
  expected_effective_structure_id: string;
  anchor_tree_node_id: string | null;
  target_review_node_ids: readonly string[];
  unselected_policy: AtomizationUnselectedPolicy;
}

export type AtomizationScopeWireHashPayload = Omit<
  AtomizationScopeWireHashInput,
  "target_review_node_ids"
> & {
  schema_version: "atomization-target-scope/v2";
  target_review_node_ids: string[];
};

interface AtomizationScopeWireCommandBase {
  schema_version: "requirement-atomization-run-request/v2";
  command_id: string;
  expected_review_revision: number;
  expected_scope_revision: number;
  expected_entry_classification_snapshot_id: string;
  expected_entry_classification_revision: number;
  expected_entry_classification_sha256: string;
  expected_working_tree_id: string;
  expected_working_tree_sha256: string;
  expected_effective_structure_id: string;
  target_scope_sha256: string;
  unselected_policy: AtomizationUnselectedPolicy;
}

export interface AtomizationApplyScopeWireCommand
  extends AtomizationScopeWireCommandBase {
  operation: "APPLY";
  scope: AtomizationScope;
  anchor_tree_node_id: string | null;
  target_review_node_ids: string[];
}

export interface AtomizationUndoScopeWireCommand
  extends AtomizationScopeWireCommandBase {
  operation: "UNDO";
  scope: "CURRENT";
  anchor_tree_node_id: null;
  target_review_node_ids: [];
}

export type AtomizationScopeWireCommand =
  | AtomizationApplyScopeWireCommand
  | AtomizationUndoScopeWireCommand;

export interface ApplyAtomizationScopeWireCommandFromPlanInput {
  plan: AtomizationScopePlan;
  session_id: string;
  command_id: string;
  expected_review_revision: number;
  expected_scope_revision: number;
  expected_entry_classification_snapshot_id: string;
  expected_entry_classification_revision: number;
  expected_entry_classification_sha256: string;
}

export interface UndoAtomizationScopeWireCommandInput {
  identity: AtomizationScopeIdentity;
  session_id: string;
  command_id: string;
  expected_review_revision: number;
  expected_scope_revision: number;
  expected_entry_classification_snapshot_id: string;
  expected_entry_classification_revision: number;
  expected_entry_classification_sha256: string;
}

export interface SelectedAtomizationWorkflowState {
  identityKey: string;
  workflowIds: string[];
}

export const ATOMIZATION_SCOPE_OPTIONS: ReadonlyArray<{
  value: AtomizationScope;
  label: string;
  description: string;
}> = [
  {
    value: "CURRENT",
    label: "当前项",
    description: "只处理当前高亮条目，不自动包含子项。",
  },
  {
    value: "SELECTED",
    label: "所选项",
    description: "只处理原子化复选框中的独立选择。",
  },
  {
    value: "BRANCH",
    label: "当前分支",
    description: "包含当前条目及其在清单中的全部下级条目。",
  },
  {
    value: "ALL",
    label: "全部条目",
    description: "检查当前清单中的全部条目。",
  },
];

export const ATOMIZATION_SCOPE_MAX_TREE_DEPTH = 512;

const CLASSIFIABLE_PROJECTION_ROLES = new Set([
  "STRUCTURE_PROXY",
  "DIRECT_CONTENT",
  "WHOLE_TABLE_REVIEW",
  "TABLE_RECORD",
  "REQUIREMENT",
]);

interface CanonicalTreeItem {
  node: RequirementTreeNode;
  pathLabel: string;
}

interface CanonicalPathSegment {
  label: string;
  parent: CanonicalPathSegment | null;
}

interface AtomizationContext {
  identity: AtomizationScopeIdentity;
  identityKey: string;
  orderedTreeItems: CanonicalTreeItem[];
  nodeById: Map<string, RequirementTreeNode>;
  classificationByTreeId: Map<string, EntryClassificationEntry>;
  canonicalWorkflowIds: string[];
}

function finiteNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value)
    ? value
    : fallback;
}

function effectiveType(
  entry: EntryClassificationEntry,
): EntryClassificationType | null {
  return entry.effective_type;
}

function canonicalTreeItems(tree: RequirementTreeSnapshot): {
  items: CanonicalTreeItem[];
  nodeById: Map<string, RequirementTreeNode>;
} {
  const nodeById = new Map<string, RequirementTreeNode>();
  for (const node of tree.nodes) {
    if (!node.id || nodeById.has(node.id)) {
      throw new Error("当前整理结果包含重复或无法识别的条目");
    }
    nodeById.set(node.id, node);
  }

  const rootIds = [...tree.root_node_ids];
  if (
    rootIds.some((nodeId) => !nodeId || !nodeById.has(nodeId)) ||
    new Set(rootIds).size !== rootIds.length
  ) {
    throw new Error("当前整理结果的顶层条目包含重复或无法识别的内容");
  }
  if (rootIds.some((nodeId) => nodeById.get(nodeId)?.parent_id !== null)) {
    throw new Error("当前整理结果的顶层条目仍关联了上级条目");
  }
  const parentlessIds = tree.nodes
    .filter((node) => node.parent_id === null)
    .map((node) => node.id);
  const rootSet = new Set(rootIds);
  if (
    rootSet.size !== parentlessIds.length ||
    parentlessIds.some((nodeId) => !rootSet.has(nodeId))
  ) {
    throw new Error("当前整理结果的顶层条目不完整");
  }

  const declaredParentByChildId = new Map<string, string>();
  for (const node of tree.nodes) {
    for (const childId of node.child_ids) {
      const child = nodeById.get(childId);
      if (
        !child ||
        child.parent_id !== node.id ||
        declaredParentByChildId.has(childId)
      ) {
        throw new Error("当前整理结果包含无法确认的上下级关系");
      }
      declaredParentByChildId.set(childId, node.id);
    }
  }
  for (const node of tree.nodes) {
    const declaredParent = declaredParentByChildId.get(node.id);
    if (
      (node.parent_id === null && declaredParent !== undefined) ||
      (node.parent_id !== null && declaredParent !== node.parent_id)
    ) {
      throw new Error("当前整理结果包含无法确认的上下级关系");
    }
  }

  const cycleChecked = new Set<string>();
  for (const node of tree.nodes) {
    if (cycleChecked.has(node.id)) continue;
    const currentPath = new Set<string>();
    let current: RequirementTreeNode | undefined = node;
    while (current && !cycleChecked.has(current.id)) {
      if (currentPath.has(current.id)) {
        throw new Error("当前整理结果包含循环的上下级关系");
      }
      currentPath.add(current.id);
      current =
        current.parent_id === null
          ? undefined
          : nodeById.get(current.parent_id);
    }
    currentPath.forEach((nodeId) => cycleChecked.add(nodeId));
  }

  const items: CanonicalTreeItem[] = [];
  const visited = new Set<string>();
  const pending = [...rootIds]
    .reverse()
    .map((nodeId) => ({
      nodeId,
      parentPath: null as CanonicalPathSegment | null,
      depth: 0,
    }));
  while (pending.length) {
    const frame = pending.pop();
    if (!frame) break;
    if (frame.depth > ATOMIZATION_SCOPE_MAX_TREE_DEPTH) {
      throw new Error(
        `当前整理结果的层级超过可处理上限 ${ATOMIZATION_SCOPE_MAX_TREE_DEPTH}`,
      );
    }
    if (visited.has(frame.nodeId)) {
      throw new Error("当前整理结果无法形成唯一的完整层级");
    }
    const node = nodeById.get(frame.nodeId);
    if (!node) throw new Error("当前整理结果包含无法读取的条目关系");
    visited.add(node.id);
    const path: CanonicalPathSegment = {
      label: node.label || node.id,
      parent: frame.parentPath,
    };
    if (node.include_in_view) {
      items.push({ node, pathLabel: materializePathLabel(path) });
    }
    for (const childId of [...node.child_ids].reverse()) {
      const child = nodeById.get(childId);
      if (!child) throw new Error("当前整理结果包含无法读取的条目关系");
      pending.push({
        nodeId: childId,
        parentPath: path,
        depth: frame.depth + 1,
      });
    }
  }
  if (visited.size !== nodeById.size) {
    throw new Error("当前整理结果包含未归入完整层级顺序的内容");
  }
  return { items, nodeById };
}

function materializePathLabel(path: CanonicalPathSegment): string {
  const labels: string[] = [];
  let current: CanonicalPathSegment | null = path;
  while (current) {
    labels.push(current.label);
    current = current.parent;
  }
  return labels.reverse().join(" › ");
}

function atomizationContext(
  tree: RequirementTreeSnapshot,
  classification: EntryClassificationResponse,
): AtomizationContext {
  const snapshot = classification.snapshot;
  if (
    tree.schema_version !== "requirement-tree-snapshot/v1" ||
    tree.mode !== "WORKING" ||
    snapshot.project_id !== tree.source_identity.project_id ||
    snapshot.sor_document_id !== tree.source_identity.document_id ||
    snapshot.source_sha256 !== tree.source_identity.source_sha256 ||
    snapshot.effective_structure_id !==
      tree.structure_identity.effective_snapshot_id
  ) {
    throw new Error("原子化范围与当前整理结果版本不一致");
  }

  const { items: orderedTreeItems, nodeById } = canonicalTreeItems(tree);
  const classificationByTreeId = new Map<string, EntryClassificationEntry>();
  const workflowIds = new Set<string>();
  for (const entry of snapshot.entries) {
    const node = nodeById.get(entry.tree_node_id);
    if (
      !node ||
      classificationByTreeId.has(entry.tree_node_id) ||
      !entry.workflow_node_id ||
      workflowIds.has(entry.workflow_node_id) ||
      (node.workflow_node_id &&
        node.workflow_node_id !== entry.workflow_node_id)
    ) {
      throw new Error("分类结果与当前整理结果中的条目不一致");
    }
    classificationByTreeId.set(entry.tree_node_id, entry);
    workflowIds.add(entry.workflow_node_id);
  }
  const expectedEntries = orderedTreeItems
    .filter(({ node }) =>
      CLASSIFIABLE_PROJECTION_ROLES.has(String(node.projection_role || "")),
    )
    .map(({ node }) => [
      node.id,
      node.workflow_node_id || node.id,
      String(node.projection_role || ""),
    ]);
  const actualEntries = snapshot.entries.map((entry) => [
    entry.tree_node_id,
    entry.workflow_node_id,
    entry.projection_role,
  ]);
  if (JSON.stringify(actualEntries) !== JSON.stringify(expectedEntries)) {
    throw new Error("分类结果与当前整理结果的完整顺序不一致");
  }

  const identity: AtomizationScopeIdentity = {
    projectId: tree.source_identity.project_id,
    documentId: tree.source_identity.document_id,
    sourceSha256: tree.source_identity.source_sha256,
    effectiveStructureId: tree.structure_identity.effective_snapshot_id,
    effectiveTreeSha256: tree.structure_identity.effective_tree_sha256,
    workingTreeId: snapshot.working_tree_id,
    workingTreeSha256: snapshot.working_tree_sha256,
  };
  const canonicalWorkflowIds = orderedTreeItems.flatMap(({ node }) => {
    const entry = classificationByTreeId.get(node.id);
    return entry ? [entry.workflow_node_id] : [];
  });
  if (canonicalWorkflowIds.length !== snapshot.entries.length) {
    throw new Error("分类结果未完整覆盖当前整理结果");
  }
  return {
    identity,
    identityKey: canonicalJson(identity),
    orderedTreeItems,
    nodeById,
    classificationByTreeId,
    canonicalWorkflowIds,
  };
}

function descendantNodeIds(
  anchorTreeNodeId: string,
  context: AtomizationContext,
): Set<string> {
  if (!anchorTreeNodeId) return new Set();
  if (!context.nodeById.has(anchorTreeNodeId)) {
    throw new Error("当前项不在最新整理结果中");
  }
  const result = new Set<string>();
  const pending = [anchorTreeNodeId];
  while (pending.length) {
    const nodeId = pending.pop();
    if (!nodeId || result.has(nodeId)) continue;
    const node = context.nodeById.get(nodeId);
    if (!node) throw new Error("当前分支包含无法读取的条目");
    result.add(nodeId);
    pending.push(...[...node.child_ids].reverse());
  }
  return result;
}

function skipReason(
  entry: EntryClassificationEntry | undefined,
  eligibleReviewNodeIds: ReadonlySet<string>,
): AtomizationScopeSkipReason | null {
  if (!entry) return "STRUCTURE";
  const type = effectiveType(entry);
  if (type === "TECHNICAL") {
    return eligibleReviewNodeIds.has(entry.workflow_node_id)
      ? null
      : "NOT_DIRECT_OR_NOT_REQUIREMENT";
  }
  if (type === "NON_TECHNICAL") return "NON_TECHNICAL";
  if (type === "INFORMATION") return "INFORMATION";
  return "UNCLASSIFIED";
}

function selectedIdsInCanonicalOrder(
  selectedWorkflowIds: readonly string[],
  canonicalWorkflowIds: readonly string[],
): string[] {
  const selected = new Set(selectedWorkflowIds.filter(Boolean));
  return canonicalWorkflowIds.filter((workflowId) => selected.has(workflowId));
}

export function buildAtomizationScopePlan(options: {
  tree: RequirementTreeSnapshot;
  classification: EntryClassificationResponse;
  scope: AtomizationScope;
  currentTreeNodeId: string;
  selectedWorkflowIds: readonly string[];
  eligibleReviewNodeIds: readonly string[];
}): AtomizationScopePlan {
  const context = atomizationContext(options.tree, options.classification);
  const eligibleReviewNodeIds = new Set(
    options.eligibleReviewNodeIds.filter(Boolean),
  );
  const selectedWorkflowIds = selectedIdsInCanonicalOrder(
    options.selectedWorkflowIds,
    context.canonicalWorkflowIds,
  );
  const selectedSet = new Set(selectedWorkflowIds);
  const descendantIds =
    options.scope === "BRANCH"
      ? descendantNodeIds(options.currentTreeNodeId, context)
      : new Set<string>();
  const scopedTreeItems = context.orderedTreeItems.filter(({ node }) => {
    if (options.scope === "ALL") return true;
    if (options.scope === "CURRENT") {
      return Boolean(options.currentTreeNodeId) && node.id === options.currentTreeNodeId;
    }
    if (options.scope === "BRANCH") return descendantIds.has(node.id);
    const entry = context.classificationByTreeId.get(node.id);
    return Boolean(entry && selectedSet.has(entry.workflow_node_id));
  });
  if (
    options.scope === "CURRENT" &&
    options.currentTreeNodeId &&
    !context.nodeById.has(options.currentTreeNodeId)
  ) {
    throw new Error("当前项不在最新整理结果中");
  }

  const items = scopedTreeItems.map<AtomizationScopeItem>(
    ({ node, pathLabel }, canonicalIndex) => {
      const entry = context.classificationByTreeId.get(node.id);
      const reason = skipReason(entry, eligibleReviewNodeIds);
      return {
        treeNodeId: node.id,
        workflowNodeId: entry?.workflow_node_id || null,
        label: node.label || node.summary || node.id,
        pathLabel,
        sourceOrder: finiteNumber(node.source_order, canonicalIndex),
        sourceOffset: finiteNumber(node.source_offset, 0),
        classificationType: entry ? effectiveType(entry) : null,
        disposition: reason ? "SKIPPED" : "TARGET",
        reason,
      };
    },
  );
  const targets = items.filter((item) => item.disposition === "TARGET");
  const skipped = items.filter((item) => item.disposition === "SKIPPED");
  return {
    identity: context.identity,
    scope: options.scope,
    anchorTreeNodeId:
      options.scope === "CURRENT" || options.scope === "BRANCH"
        ? options.currentTreeNodeId || null
        : null,
    selectedWorkflowIds:
      options.scope === "SELECTED" ? selectedWorkflowIds : [],
    items,
    requestedTreeNodeIds: items.map((item) => item.treeNodeId),
    requestedWorkflowIds: items.flatMap((item) =>
      item.workflowNodeId ? [item.workflowNodeId] : [],
    ),
    executableWorkflowIds: targets.flatMap((item) =>
      item.workflowNodeId ? [item.workflowNodeId] : [],
    ),
    estimatedRemoteCallCount: targets.length,
    targets,
    skipped,
  };
}

export function reconcileSelectedAtomizationWorkflowIds(
  previous: SelectedAtomizationWorkflowState,
  tree: RequirementTreeSnapshot,
  classification: EntryClassificationResponse,
): SelectedAtomizationWorkflowState {
  const context = atomizationContext(tree, classification);
  return {
    identityKey: context.identityKey,
    workflowIds:
      previous.identityKey === context.identityKey
        ? selectedIdsInCanonicalOrder(
            previous.workflowIds,
            context.canonicalWorkflowIds,
          )
        : [],
  };
}

export function atomizationPreviewFingerprintPayload(
  plan: AtomizationScopePlan,
): AtomizationPreviewFingerprintPayload {
  return {
    identity: { ...plan.identity },
    scope: plan.scope,
    anchorTreeNodeId: plan.anchorTreeNodeId,
    selectedWorkflowIds: [...plan.selectedWorkflowIds],
    requestedTreeNodeIds: [...plan.requestedTreeNodeIds],
    requestedWorkflowIds: [...plan.requestedWorkflowIds],
    executableWorkflowIds: [...plan.executableWorkflowIds],
    skipped: plan.skipped.map((item) => ({
      treeNodeId: item.treeNodeId,
      workflowNodeId: item.workflowNodeId,
      reason: item.reason || "STRUCTURE",
    })),
  };
}

export async function sha256AtomizationPreviewFingerprint(
  payload: AtomizationPreviewFingerprintPayload,
): Promise<string> {
  return sha256CanonicalPayload(payload);
}

export function atomizationScopeWireHashPayload(
  input: AtomizationScopeWireHashInput,
): AtomizationScopeWireHashPayload {
  return {
    schema_version: "atomization-target-scope/v2",
    project_id: input.project_id,
    document_id: input.document_id,
    session_id: input.session_id,
    command_id: input.command_id,
    operation: input.operation,
    scope: input.scope,
    expected_review_revision: input.expected_review_revision,
    expected_scope_revision: input.expected_scope_revision,
    expected_entry_classification_snapshot_id:
      input.expected_entry_classification_snapshot_id,
    expected_entry_classification_revision:
      input.expected_entry_classification_revision,
    expected_entry_classification_sha256:
      input.expected_entry_classification_sha256,
    expected_working_tree_id: input.expected_working_tree_id,
    expected_working_tree_sha256: input.expected_working_tree_sha256,
    expected_effective_structure_id: input.expected_effective_structure_id,
    anchor_tree_node_id: input.anchor_tree_node_id,
    target_review_node_ids: [...input.target_review_node_ids],
    unselected_policy: input.unselected_policy,
  };
}

export async function sha256AtomizationScopeWirePayload(
  payload: AtomizationScopeWireHashPayload,
): Promise<string> {
  return sha256CanonicalPayload(payload);
}

export async function buildApplyAtomizationScopeWireCommandFromPlan(
  input: ApplyAtomizationScopeWireCommandFromPlanInput,
): Promise<AtomizationApplyScopeWireCommand> {
  assertExecutableApplyPlan(input.plan);
  const hashInput: AtomizationScopeWireHashInput = {
    project_id: input.plan.identity.projectId,
    document_id: input.plan.identity.documentId,
    session_id: input.session_id,
    command_id: input.command_id,
    operation: "APPLY",
    scope: input.plan.scope,
    expected_review_revision: input.expected_review_revision,
    expected_scope_revision: input.expected_scope_revision,
    expected_entry_classification_snapshot_id:
      input.expected_entry_classification_snapshot_id,
    expected_entry_classification_revision:
      input.expected_entry_classification_revision,
    expected_entry_classification_sha256:
      input.expected_entry_classification_sha256,
    expected_working_tree_id: input.plan.identity.workingTreeId,
    expected_working_tree_sha256: input.plan.identity.workingTreeSha256,
    expected_effective_structure_id:
      input.plan.identity.effectiveStructureId,
    anchor_tree_node_id: input.plan.anchorTreeNodeId,
    target_review_node_ids: input.plan.requestedWorkflowIds,
    unselected_policy: "SOURCE_BOUND_PASSTHROUGH",
  };
  const targetScopeSha256 = await sha256AtomizationScopeWirePayload(
    atomizationScopeWireHashPayload(hashInput),
  );
  return {
    schema_version: "requirement-atomization-run-request/v2",
    command_id: input.command_id,
    operation: "APPLY",
    scope: input.plan.scope,
    expected_review_revision: input.expected_review_revision,
    expected_scope_revision: input.expected_scope_revision,
    expected_entry_classification_snapshot_id:
      input.expected_entry_classification_snapshot_id,
    expected_entry_classification_revision:
      input.expected_entry_classification_revision,
    expected_entry_classification_sha256:
      input.expected_entry_classification_sha256,
    expected_working_tree_id: input.plan.identity.workingTreeId,
    expected_working_tree_sha256: input.plan.identity.workingTreeSha256,
    expected_effective_structure_id:
      input.plan.identity.effectiveStructureId,
    anchor_tree_node_id: input.plan.anchorTreeNodeId,
    target_review_node_ids: [...input.plan.requestedWorkflowIds],
    target_scope_sha256: targetScopeSha256,
    unselected_policy: "SOURCE_BOUND_PASSTHROUGH",
  };
}

export async function buildUndoAtomizationScopeWireCommand(
  input: UndoAtomizationScopeWireCommandInput,
): Promise<AtomizationUndoScopeWireCommand> {
  const hashInput: AtomizationScopeWireHashInput = {
    project_id: input.identity.projectId,
    document_id: input.identity.documentId,
    session_id: input.session_id,
    command_id: input.command_id,
    operation: "UNDO",
    scope: "CURRENT",
    expected_review_revision: input.expected_review_revision,
    expected_scope_revision: input.expected_scope_revision,
    expected_entry_classification_snapshot_id:
      input.expected_entry_classification_snapshot_id,
    expected_entry_classification_revision:
      input.expected_entry_classification_revision,
    expected_entry_classification_sha256:
      input.expected_entry_classification_sha256,
    expected_working_tree_id: input.identity.workingTreeId,
    expected_working_tree_sha256: input.identity.workingTreeSha256,
    expected_effective_structure_id: input.identity.effectiveStructureId,
    anchor_tree_node_id: null,
    target_review_node_ids: [],
    unselected_policy: "SOURCE_BOUND_PASSTHROUGH",
  };
  const targetScopeSha256 = await sha256AtomizationScopeWirePayload(
    atomizationScopeWireHashPayload(hashInput),
  );
  return {
    schema_version: "requirement-atomization-run-request/v2",
    command_id: input.command_id,
    operation: "UNDO",
    scope: "CURRENT",
    expected_review_revision: input.expected_review_revision,
    expected_scope_revision: input.expected_scope_revision,
    expected_entry_classification_snapshot_id:
      input.expected_entry_classification_snapshot_id,
    expected_entry_classification_revision:
      input.expected_entry_classification_revision,
    expected_entry_classification_sha256:
      input.expected_entry_classification_sha256,
    expected_working_tree_id: input.identity.workingTreeId,
    expected_working_tree_sha256: input.identity.workingTreeSha256,
    expected_effective_structure_id: input.identity.effectiveStructureId,
    anchor_tree_node_id: null,
    target_review_node_ids: [],
    target_scope_sha256: targetScopeSha256,
    unselected_policy: "SOURCE_BOUND_PASSTHROUGH",
  };
}

function assertExecutableApplyPlan(plan: AtomizationScopePlan): void {
  if (plan.executableWorkflowIds.length === 0) {
    throw new Error("当前范围没有可拆分的技术需求");
  }
  const requested = new Set(plan.requestedWorkflowIds);
  if (
    requested.size !== plan.requestedWorkflowIds.length ||
    new Set(plan.executableWorkflowIds).size !==
      plan.executableWorkflowIds.length ||
    plan.executableWorkflowIds.some((workflowId) => !requested.has(workflowId))
  ) {
    throw new Error("当前范围的数据已变化，请重新读取后再试");
  }
  const needsAnchor = plan.scope === "CURRENT" || plan.scope === "BRANCH";
  if (needsAnchor !== Boolean(plan.anchorTreeNodeId)) {
    throw new Error("当前范围与所选当前项不一致");
  }
  if (plan.scope === "CURRENT" && plan.requestedWorkflowIds.length !== 1) {
    throw new Error("“当前项”范围必须且只能包含一个条目");
  }
}

async function sha256CanonicalPayload(payload: unknown): Promise<string> {
  if (!globalThis.crypto?.subtle) {
    throw new Error("当前浏览器无法完成范围校验，请升级浏览器后重试");
  }
  const bytes = new TextEncoder().encode(canonicalJson(payload));
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
}

export function atomizationScopeViewModel(
  plan: AtomizationScopePlan,
): AtomizationScopeViewModel {
  const targetCount = plan.targets.length;
  const skippedCount = plan.skipped.length;
  const estimatedRemoteCallCount = plan.estimatedRemoteCallCount;
  const toViewItem = (item: AtomizationScopeItem): AtomizationScopeViewItem => ({
    ...item,
    reasonLabel: item.reason ? skipReasonLabel(item.reason) : "",
  });
  return {
    plan,
    scope: plan.scope,
    scopeLabel:
      ATOMIZATION_SCOPE_OPTIONS.find((option) => option.value === plan.scope)
        ?.label || plan.scope,
    defaultBehavior: "默认保留原条目，不拆分",
    summary: `${targetCount} 项可执行，${skippedCount} 项保留/跳过，预计 AI 最多处理 ${estimatedRemoteCallCount} 次`,
    actionLabel:
      targetCount > 0 ? `原子化 ${targetCount} 项技术需求` : "执行原子化",
    targetCount,
    skippedCount,
    estimatedRemoteCallCount,
    canApply: targetCount > 0,
    emptyReason:
      targetCount > 0 ? "" : "当前范围没有可原子化的技术需求",
    targets: plan.targets.map(toViewItem),
    skipped: plan.skipped.map(toViewItem),
  };
}

export function atomizationAttemptActionGate(
  plan: AtomizationScopePlan,
  lastAttempt: RequirementAtomizationScopeAttempt | null,
): AtomizationAttemptActionGate {
  const ready: AtomizationAttemptActionGate = {
    serverRunning: false,
    openScopeBlocked: false,
    applyBlocked: false,
    undoBlocked: false,
    retryingApply: false,
    guidance: "",
  };
  if (!lastAttempt || lastAttempt.status === "SUCCEEDED") return ready;
  if (lastAttempt.status === "RUNNING") {
    const operationLabel =
      lastAttempt.operation === "UNDO" ? "撤销原子化" : "执行原子化";
    return {
      serverRunning: true,
      openScopeBlocked: true,
      applyBlocked: true,
      undoBlocked: true,
      retryingApply: false,
      guidance: `系统正在${operationLabel}；每 2.5 秒自动更新进度，完成前不可发起新操作。`,
    };
  }
  if (lastAttempt.operation === "UNDO") {
    return lastAttempt.retryable
      ? {
          ...ready,
          guidance: "上次撤销已中断，可重试撤销。",
        }
      : {
          ...ready,
          undoBlocked: true,
          guidance: "最近一次撤销不可重试；当前不可再次撤销。",
        };
  }

  const matchesCanonicalRequest =
    lastAttempt.scope === plan.scope &&
    lastAttempt.requested_review_node_ids.length ===
      plan.requestedWorkflowIds.length &&
    lastAttempt.requested_review_node_ids.every(
      (reviewNodeId, index) =>
        reviewNodeId === plan.requestedWorkflowIds[index],
    );
  if (!matchesCanonicalRequest) return ready;
  if (lastAttempt.retryable) {
    return {
      ...ready,
      retryingApply: true,
      guidance: "上次执行已中断，可重试当前范围。",
    };
  }
  return {
    ...ready,
    applyBlocked: true,
    guidance: "最近一次执行不可重试；请调整范围后再执行。",
  };
}

export function atomizationSelectionIdentityKey(
  tree: RequirementTreeSnapshot,
  classification: EntryClassificationResponse,
): string {
  return atomizationContext(tree, classification).identityKey;
}

export function skipReasonLabel(reason: AtomizationScopeSkipReason): string {
  const labels: Record<AtomizationScopeSkipReason, string> = {
    STRUCTURE: "结构行不参与原子化",
    UNCLASSIFIED: "尚未分类，保持原条目",
    NON_TECHNICAL: "非技术需求不参与原子化",
    INFORMATION: "信息与参考不参与原子化",
    NOT_DIRECT_OR_NOT_REQUIREMENT: "不是直接需求或需求节点，保持原条目",
  };
  return labels[reason];
}

function canonicalJson(value: unknown): string {
  const stack = new Set<object>();
  const normalize = (current: unknown): unknown => {
    if (
      current === null ||
      typeof current === "string" ||
      typeof current === "boolean"
    ) {
      return current;
    }
    if (typeof current === "number") {
      if (!Number.isFinite(current)) {
        throw new Error("范围校验数据包含无效数字");
      }
      return current;
    }
    if (Array.isArray(current)) {
      if (stack.has(current)) throw new Error("范围校验数据存在循环引用");
      stack.add(current);
      const result = current.map(normalize);
      stack.delete(current);
      return result;
    }
    if (typeof current === "object" && current) {
      if (stack.has(current)) throw new Error("范围校验数据存在循环引用");
      stack.add(current);
      const record = current as Record<string, unknown>;
      const result: Record<string, unknown> = {};
      for (const key of Object.keys(record).sort()) {
        if (record[key] === undefined) {
          throw new Error("范围校验数据存在未定义内容");
        }
        result[key] = normalize(record[key]);
      }
      stack.delete(current);
      return result;
    }
    throw new Error("范围校验数据包含不支持的内容");
  };
  return JSON.stringify(normalize(value));
}
