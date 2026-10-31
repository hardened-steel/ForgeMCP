import { connectWidget } from "./shared/app.js";
import { isObject } from "./shared/presentation.js";

/** Show one immutable debugger result with the common complete fields and JSON views. */
function debuggerPresentation(data, toolName) {
  const collections = [
    ["sessions", "session_id", "phase"],
    ["threads", "name", "state"],
    ["frames", "name"],
    ["scopes", "name"],
    ["variables", "name", "type"],
    ["modules", "name", "symbol_status"],
    ["breakpoints", "id"],
    ["instructions", "address"],
    ["items", "name"],
    ["result", "session_id"],
  ];
  if (isObject(data)) {
    for (const [key, titleKey, categoryKey] of collections) {
      if (Array.isArray(data[key])) {
        return {
          toolName,
          summary: Object.fromEntries(Object.entries(data).filter(([name]) => name !== key)),
          records: data[key],
          titleKey,
          categoryKey,
        };
      }
    }
  }
  return { toolName, summary: data, records: null };
}

await connectWidget({
  toolName: "debugger_sessions",
  describe: (data, resources, toolName) => debuggerPresentation(data, toolName),
});
