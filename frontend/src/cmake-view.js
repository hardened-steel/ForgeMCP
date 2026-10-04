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
      value === null ? "Succeeded" : value === "" ? "Empty error string" : value);
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
      const fields = Object.fromEntries(Object.entries(test).filter(([name]) => name !== "name" && name !== "status"));
      details.append(summary, valueNode(fields));
      list.append(details);
    }
    return list;
  }
  return null;
}
