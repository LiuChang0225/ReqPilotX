import { asRecord } from "../../shared/api/client";
import type { JsonRecord } from "../../shared/api/contracts";

export function requirementPublicationSetId(
  result: JsonRecord | null,
): string {
  const publication = asRecord(asRecord(result).publication);
  return String(
    publication.requirement_set_id ||
      asRecord(publication.requirement_set).id ||
      "",
  );
}

export function draftRequirementPublicationSetId(
  result: JsonRecord | null,
): string {
  const publication = asRecord(asRecord(result).publication);
  const status = String(
    publication.status ||
      publication.requirement_set_status ||
      asRecord(publication.requirement_set).status ||
      "",
  ).toUpperCase();
  return status === "DRAFT" ? requirementPublicationSetId(result) : "";
}

export function requirementPublicationNotice(result: JsonRecord | null): string {
  const response = asRecord(result);
  const publication = asRecord(response.publication);
  const created =
    response.requirement_set_created ??
    publication.requirement_set_created;
  if (created === false) {
    const version = String(
      publication.version ||
        response.requirement_set_version ||
        asRecord(publication.requirement_set).version ||
        "",
    );
    return version
      ? `已复用 v${version}，没有创建新版本`
      : "已复用现有审阅版本，没有创建新版本";
  }
  return "审阅版本已生成";
}
