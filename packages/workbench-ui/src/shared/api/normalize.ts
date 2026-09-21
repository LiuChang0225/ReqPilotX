import type {
  AiProfile,
  JsonRecord,
  RuntimeCheck,
} from "./contracts";

function recordsFromCollection(value: unknown): JsonRecord[] {
  if (Array.isArray(value)) {
    return value.filter(
      (item): item is JsonRecord =>
        Boolean(item && typeof item === "object" && !Array.isArray(item)),
    );
  }
  if (!value || typeof value !== "object") return [];
  return Object.entries(value as JsonRecord)
    .filter(
      (entry): entry is [string, JsonRecord] =>
        Boolean(
          entry[1] &&
            typeof entry[1] === "object" &&
            !Array.isArray(entry[1]),
        ),
    )
    .map(([key, item]) => ({
      ...item,
      id: item.id || key,
      check_id: item.check_id || key,
    }));
}

export function normalizeAiProfiles(value: unknown): AiProfile[] {
  return recordsFromCollection(value).filter(
    (item): item is AiProfile =>
      typeof item.id === "string" && item.id.length > 0,
  );
}

export function normalizeRuntimeChecks(value: unknown): RuntimeCheck[] {
  return recordsFromCollection(value).filter(
    (item): item is RuntimeCheck =>
      typeof item.status === "string" && item.status.length > 0,
  );
}
