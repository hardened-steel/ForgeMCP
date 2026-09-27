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
  const uri = data?.extensions_uri;
  if (!uri || result.isError || !("lines_added" in data || "replacements" in data)) return null;
  if (!/^forgemcp:\/\/workspace\/results\/[a-zA-Z0-9_-]+\/extensions\.json$/.test(uri)) throw new Error("Invalid manifest link");
  const manifest = decode(await read({ uri }), uri);
  if (!current()) return null;
  const extension = manifest.extensions?.find((item) => item.kind === "diff" && item.version === 1);
  if (!extension) return null;
  if (extension.error) throw new Error("Diff provider failed");
  const name = extension.data?.resource;
  if (typeof name !== "string" || !/^[a-zA-Z0-9_-]+\.json$/.test(name)) throw new Error("Invalid diff link");
  const diffUri = uri.slice(0, -"extensions.json".length) + name;
  const linked = (links) => links?.some((link) => link.uri === diffUri && link.mime_type === "application/json");
  if (!linked(data.resources) || !linked(manifest.resources)) throw new Error("Diff resource is not linked");
  const diff = decode(await read({ uri: diffUri }), diffUri);
  return current() ? validateDiff(diff, data.path) : null;
}
