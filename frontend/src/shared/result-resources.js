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
  const uri = resultResourceUri(link, "diff");
  if (!current()) return null;
  const diff = decode(await read({ uri }), uri);
  return current() ? validateDiff(diff, data.path) : null;
}

function resultResourceUri(link, provider) {
  if (link?.mime_type !== "application/json" || typeof link.uri !== "string"
    || !new RegExp(`^forgemcp://workspace/results/[0-9a-f]{32}/${provider}\\.json$`).test(link.uri)) {
    throw new Error("Invalid result resource link");
  }
  return link.uri;
}

export async function loadResultSkippedFiles(result, read, current = () => true) {
  const link = result?.structuredContent?.resources?.skipped_files;
  if (!link || result.isError) return null;
  const uri = resultResourceUri(link, "skipped_files");
  if (!current()) return null;
  const data = decode(await read({ uri }), uri);
  if (!Array.isArray(data?.skipped_files) || !data.skipped_files.every((path) =>
    typeof path === "string" && /^(project|storage)\//.test(path))
    || data.skipped_files.length !== result.structuredContent.skipped_files_count) {
    throw new Error("Invalid skipped-file resource");
  }
  return current() ? data : null;
}

export async function loadResultClangd(result, read, current = () => true) {
  const link = result?.structuredContent?.resources?.clangd;
  if (!link || result.isError) return null;
  const uri = resultResourceUri(link, "clangd");
  if (!current()) return null;
  const analysis = decode(await read({ uri }), uri);
  if (!current()) return null;
  const position = (value) => value && Number.isInteger(value.line) && value.line > 0
    && Number.isInteger(value.character) && value.character >= 0;
  const range = (value) => position(value?.start) && position(value?.end)
    && (value.end.line > value.start.line || value.end.line === value.start.line && value.end.character >= value.start.character);
  const configured = (group) => group && Array.isArray(group.configurations)
    && group.configurations.length > 0 && group.configurations.every((id) => typeof id === "string");
  if (analysis?.version !== 1 || !Array.isArray(analysis.files) || !analysis.files.every((file) =>
    typeof file.path === "string" && /^(project|storage)\//.test(file.path)
    && Array.isArray(file.diagnostics) && file.diagnostics.every((group) => configured(group) && group.path === file.path
      && Array.isArray(group.diagnostics) && group.diagnostics.every((item) => range(item?.range) && typeof item.message === "string"))
    && Array.isArray(file.highlighting) && file.highlighting.every((group) => configured(group) && group.path === file.path
      && Array.isArray(group.spans) && group.spans.every((item) => range(item?.range) && typeof item.kind === "string"
        && Array.isArray(item.modifiers) && item.modifiers.every((modifier) => typeof modifier === "string"))))) {
    throw new Error("Invalid clangd resource");
  }
  return analysis;
}
