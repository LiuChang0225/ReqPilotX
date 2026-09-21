const JSON_NUMBER_SOURCES = Symbol("reqpilotx-json-number-sources");

type JsonContainer = Record<PropertyKey, unknown> & {
  [JSON_NUMBER_SOURCES]?: Map<string, string>;
};

type SourceAwareReviver = (
  this: JsonContainer,
  key: string,
  value: unknown,
  context?: { source?: string },
) => unknown;

let sourceAwareJsonParseAvailable: boolean | undefined;

function supportsSourceAwareJsonParse(): boolean {
  if (sourceAwareJsonParseAvailable !== undefined) {
    return sourceAwareJsonParseAvailable;
  }
  let preserved = "";
  const parse = JSON.parse as unknown as (
    source: string,
    reviver: SourceAwareReviver,
  ) => unknown;
  parse('{"value":1.0}', function inspectSource(key, value, context) {
    if (key === "value") preserved = context?.source || "";
    return value;
  });
  sourceAwareJsonParseAvailable = preserved === "1.0";
  return sourceAwareJsonParseAvailable;
}

/**
 * Preserve the number tokens from an API response without changing the parsed
 * JSON value. Python's canonical JSON deliberately distinguishes `1` from
 * `1.0` and writes small exponents as `1e-07`; normal JSON.parse would erase
 * that spelling before the browser verifies a publication hash.
 */
export function parseJsonWithNumberSources(text: string): unknown {
  if (!supportsSourceAwareJsonParse()) {
    throw new Error(
      "当前浏览器无法校验确认清单，请升级浏览器后重试。",
    );
  }
  const parse = JSON.parse as unknown as (
    source: string,
    reviver: SourceAwareReviver,
  ) => unknown;
  return parse(text, function preserveNumberSource(key, value, context) {
    const source = context?.source;
    if (typeof value === "number" && source) {
      let sources = this[JSON_NUMBER_SOURCES];
      if (!sources) {
        sources = new Map<string, string>();
        Object.defineProperty(this, JSON_NUMBER_SOURCES, {
          configurable: false,
          enumerable: false,
          value: sources,
          writable: false,
        });
      }
      sources.set(key, source);
    }
    return value;
  });
}

export function jsonNumberSource(
  container: object,
  key: string,
): string | undefined {
  return (container as JsonContainer)[JSON_NUMBER_SOURCES]?.get(key);
}
