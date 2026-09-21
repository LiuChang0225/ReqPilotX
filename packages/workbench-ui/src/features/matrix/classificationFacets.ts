import type { RequirementMatrixNode } from "../../shared/api/contracts";

export interface ClassificationFacetFilter { category: string; topic: string }
export const EMPTY_CLASSIFICATION_FILTER: ClassificationFacetFilter = { category: "", topic: "" };
export const CLASSIFICATION_OPTIONS = [
  ["", "全部需求类别"], ["PRODUCT_SYSTEM", "产品／系统 · 全部"],
  ["PRODUCT_SYSTEM.FUNCTIONAL", "产品／系统 · 功能"],
  ["PRODUCT_SYSTEM.QUALITY", "产品／系统 · 质量"],
  ["PRODUCT_SYSTEM.CONSTRAINT", "产品／系统 · 约束"],
  ["PROJECT", "项目要求"], ["PROCESS", "过程要求"],
] as const;
export function classificationLabels(node: RequirementMatrixNode): string[] {
  return (node.classification_paths || []).map(value => CLASSIFICATION_OPTIONS.find(([key]) => key === value)?.[1] || value);
}
export function matchesClassificationFacets(node: RequirementMatrixNode, filter: ClassificationFacetFilter): boolean {
  const classes = node.classification_paths || [];
  const category = !filter.category || (filter.category === "PRODUCT_SYSTEM"
    ? classes.some(value => value.startsWith("PRODUCT_SYSTEM."))
    : classes.includes(filter.category));
  return category && (!filter.topic || (node.topic_tags || []).includes(filter.topic));
}
