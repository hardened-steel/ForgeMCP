import { isObject, statusTone } from "./shared/presentation.js";

/** Preserve the original records; the shared view owns filtering, JSON, and copying. */
export function cmakePresentation(data, toolName) {
  const records = Array.isArray(data) ? data : isObject(data) && Array.isArray(data.result) ? data.result : null;
  return {
    toolName,
    summary: records === null ? data : Array.isArray(data) ? {} : Object.fromEntries(
      Object.entries(data).filter(([key]) => key !== "result"),
    ),
    records,
    titleKey: toolName === "cmake_profiles" ? "name" : "profile",
    categoryKey: toolName === "cmake_profiles" ? "mode" : "profile",
  };
}

export function renderCMakeValue(value, key, record, { doc, valueNode }) {
  const node = (tag, className, text) => {
    const element = doc.createElement(tag);
    element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  if (key === "error" && (value === null || typeof value === "string")) {
    return node("span", `fm-value ${value === null ? "fm-success" : "fm-error"}`,
      value === null ? "Succeeded (null)" : value === "" ? "Empty error string" : value);
  }
  if (key === "output_tail" && typeof value === "string") {
    if (!value) return node("span", "fm-value fm-null", "No command output (empty string)");
    const details = node("details", "fm-source");
    details.open = Boolean(record?.error);
    details.append(
      node("summary", "fm-label", "Command output (tail)"),
      node("pre", "fm-json", value),
    );
    return details;
  }
  if (key === "tests" && Array.isArray(value)) {
    if (!value.length) return node("span", "fm-value fm-null", "No test cases in this result");
    const list = node("div", "fm-nested");
    for (const test of value) {
      if (!isObject(test)) {
        list.append(valueNode(test));
        continue;
      }
      const details = node("details", "fm-record");
      details.open = true;
      const summary = node("summary", "fm-record-heading");
      summary.append(
        node("span", statusTone(test.status, test), String(test.status ?? "Unknown status")),
        doc.createTextNode(` · ${test.name ?? "Unnamed test"}`),
      );
      details.append(summary, valueNode(test));
      list.append(details);
    }
    return list;
  }
  return null;
}
