/** Pure formatting and snapshot projections. No SDK, DOM, or server access. */
export const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

export function copyText(value) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

export function statusTone(status, record) {
  if (status === "failed" || status === "timed_out") return "fm-error";
  if (status === "exited") {
    if (record?.return_code === 0) return "fm-success";
    return typeof record?.return_code === "number" ? "fm-error" : "";
  }
  if (status === "terminated") return "fm-warning";
  if (status === "running" || status === "starting") return "fm-active";
  return "";
}

const dates = new Intl.DateTimeFormat("en-GB", {
  day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  second: "2-digit", hourCycle: "h23", timeZone: "UTC",
});

export function timestamp(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return { readable: `${dates.format(date)} UTC`, iso: date.toISOString().replace(/\.\d{3}Z$/, "Z") };
}

export function jsonTokens(data) {
  const text = JSON.stringify(data, null, 2);
  const expression = /"(?:\\.|[^"\\])*"|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}\[\],:]/g;
  const tokens = [];
  let cursor = 0;
  for (const match of text.matchAll(expression)) {
    if (match.index > cursor) tokens.push({ text: text.slice(cursor, match.index), tone: "" });
    const token = match[0];
    let tone = "fm-json-punctuation";
    if (token.startsWith('"')) tone = /^\s*:/.test(text.slice(match.index + token.length)) ? "fm-json-key" : "fm-json-string";
    else if (token === "true" || token === "false") tone = "fm-boolean";
    else if (token === "null") tone = "fm-null";
    else if (/^-?\d/.test(token)) tone = "fm-number";
    tokens.push({ text: token, tone });
    cursor = match.index + token.length;
  }
  if (cursor < text.length) tokens.push({ text: text.slice(cursor), tone: "" });
  return tokens;
}

function collection(data, key, toolName, titleKey, categoryKey = null) {
  if (!isObject(data) || !Array.isArray(data[key])) return { toolName, summary: data, records: null };
  return {
    toolName,
    summary: Object.fromEntries(Object.entries(data).filter(([name]) => name !== key)),
    records: data[key], titleKey, categoryKey,
  };
}

export function workspacePresentation(data) {
  return { toolName: "workspace_overview", summary: data, records: null };
}

export function processPresentation(data) {
  return collection(data, "processes", "processes_overview", "status", "status");
}

export function toolsetPresentation(data) {
  // A list result has summaries only. A detail result has tools from that one toolset only.
  if (isObject(data) && Array.isArray(data.result)) return collection(data, "result", "toolsets_list", "name");
  return collection(data, "tools", "toolset_get", "name", "kind");
}
