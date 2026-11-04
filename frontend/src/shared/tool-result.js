import { isObject } from "./presentation.js";

/** Recover one complete JSON object when a host replays only text content. */
export function restoreStructuredContent(result) {
  if (!result || result.isError || result.structuredContent != null) return result;
  let recovered;
  for (const item of result.content ?? []) {
    if (item.type !== "text" || typeof item.text !== "string") continue;
    const text = item.text.trim();
    const fenced = /^```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```$/i.exec(text);
    let value;
    try {
      value = JSON.parse(fenced ? fenced[1] : text);
    } catch {
      continue;
    }
    if (!isObject(value)) continue;
    if (recovered !== undefined) return result;
    recovered = value;
  }
  return recovered === undefined ? result : { ...result, structuredContent: recovered };
}
