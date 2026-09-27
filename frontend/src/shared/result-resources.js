/** Read only immutable resources explicitly linked by this tool result. */
function decode(response, uri) {
  const content = response?.contents?.find((item) => item.uri === uri && item.mimeType === "application/json" && typeof item.text === "string");
  if (!content) throw new Error("Missing JSON resource");
  return JSON.parse(content.text);
}

export function validateDiff(diff, path) {
  const integer = (value) => Number.isInteger(value) && value >= 0;
  const range = (value) => value && integer(value.before_start) && value.before_start > 0
    && integer(value.before_count) && integer(value.after_start) && value.after_start > 0 && integer(value.after_count);
  if (!diff || diff.path !== path || !Array.isArray(diff.changes) || !Array.isArray(diff.hunks)
    || !diff.changes.every((change) => range(change) && ["replace", "insert", "delete"].includes(change.kind))
    || !diff.hunks.every((hunk) => range(hunk) && Array.isArray(hunk.lines) && hunk.lines.every((line) => {
      if (!line || typeof line.text !== "string") return false;
      let cursor = 0;
      if (line.spans !== undefined && (!Array.isArray(line.spans) || !line.spans.every((span) => {
        if (!Array.isArray(span) || span.length !== 2 || !integer(span[0]) || !integer(span[1])
          || span[0] < cursor || span[1] < span[0] || span[1] > Array.from(line.text).length) return false;
        cursor = span[1];
        return true;
      }))) return false;
      const before = integer(line.before_line) && line.before_line > 0;
      const after = integer(line.after_line) && line.after_line > 0;
      return line.kind === "context" ? before && after
        : line.kind === "added" ? line.before_line === null && after
        : line.kind === "removed" && before && line.after_line === null;
    }))) throw new Error("Invalid diff resource");
  return diff;
}

export async function loadResultDiff(result, read, current = () => true) {
  const data = result?.structuredContent;
  const link = data?.resources?.diff;
  if (!link || result.isError || !("lines_added" in data || "replacements" in data)) return null;
  if (link.version !== undefined && link.version !== 1) return null;
  const uri = link.uri;
  if (typeof uri !== "string" || !/^forgemcp:\/\/workspace\/results\/[a-zA-Z0-9_-]+\/[a-zA-Z0-9_-]+\.json$/.test(uri)
    || link.mime_type !== "application/json") throw new Error("Invalid diff link");
  if (!current()) return null;
  const diff = decode(await read({ uri }), uri);
  return current() ? validateDiff(diff, data.path) : null;
}
