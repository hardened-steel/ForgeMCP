import test from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import { createResultView } from "../src/shared/result-view.js";
import { jsonTokens, timestamp, statusTone, processPresentation, toolsetPresentation } from "../src/shared/presentation.js";
import { workspacePresentation } from "../src/workspace-view.js";

function mount(t, describe, data) {
  const dom = new JSDOM('<main id="widget"></main>');
  t.after(() => dom.window.close());
  const root = dom.window.document.querySelector("main");
  const view = createResultView(root, { toolName: "test", describe });
  if (data !== undefined) view.receive({ structuredContent: data });
  return { root, view, win: dom.window, content: root.querySelector(".fm-scroll") };
}
const click = (root, label) => [...root.querySelectorAll("button")].find(
  (node) => node.getAttribute("aria-label") === label || node.textContent === label,
).click();
const tick = () => new Promise((resolve) => setImmediate(resolve));

test("every copy button has inline SVG geometry without loading an image or requiring hover", (t) => {
  for (const [describe, data] of [
    [workspacePresentation, { name: "Workspace" }],
    [processPresentation, { processes: [{ status: "running" }] }],
    [toolsetPresentation, { result: [{ name: "System", tools: ["cmake"] }] }],
  ]) {
    const { root } = mount(t, describe, data);
    const buttons = root.querySelectorAll(".fm-copy, .fm-copy-all");
    assert.ok(buttons.length >= 2);
    for (const button of buttons) {
      const svg = button.querySelector("svg.fm-icon");
      assert.ok(svg);
      assert.equal(svg.namespaceURI, "http://www.w3.org/2000/svg");
      assert.equal(svg.getAttribute("stroke"), "currentColor");
      assert.equal(svg.getAttribute("fill"), "none");
      assert.equal(svg.getAttribute("width"), "14");
      assert.equal(svg.getAttribute("height"), "14");
      assert.equal(svg.getAttribute("aria-hidden"), "true");
      assert.ok(svg.querySelector("rect"));
      assert.ok(svg.querySelector("path").getAttribute("d"));
      assert.equal(svg.querySelector("image, use"), null);
      assert.match(button.getAttribute("aria-label"), /^Copy /);
    }
  }
});

test("workspace metadata displays every field and unknown fields", (t) => {
  const data = { root: "project", path: "src/math.cpp", size_bytes: 903,
    created_at: null, modified_at: "2026-09-19T10:00:00Z", owner: "user",
    future_field: "also visible" };
  const { root, content } = mount(t, workspacePresentation, data);
  assert.deepEqual([...root.querySelectorAll("dt")].map((node) => node.textContent), Object.keys(data));
  assert.match(content.textContent, /903/);
  assert.match(content.textContent, /also visible/);
  assert.equal(content.querySelector(".fm-null").textContent, "null");
});

test("process rows preserve long paths, arguments and decoding flag as separate cells", (t) => {
  const path = "C:/" + "long directory/".repeat(200) + "cmake.exe";
  const data = { running: 0, completed: 1, processes: [{ process_id: "p1",
    executable: path, arguments: ["--build", "directory with spaces", "λ"],
    status: "timed_out", return_code: null, had_decoding_errors: false,
    started_at: "2026-09-07T15:45:12.123456789+03:00", finished_at: null }] };
  const { root, content } = mount(t, processPresentation, data);
  assert.ok(content.textContent.includes(path));
  assert.ok(content.textContent.includes("--build, directory with spaces, λ"));
  const label = [...root.querySelectorAll("dt")].find((node) => node.textContent === "had_decoding_errors");
  assert.equal(label.nextElementSibling.textContent, "false");
  assert.equal(label.parentElement.children.length, 3);
  assert.equal(content.querySelector(".fm-error").textContent, "timed_out");
  assert.match(content.textContent, /7 Sept 2026, 12:45:12 UTC \(2026-09-07T12:45:12Z\)/);
  assert.ok(!content.textContent.includes("123456789"));
  assert.equal(root.querySelector("time").dateTime, data.processes[0].started_at);
});

test("toolset summaries display complete compact lists and no invented detail", (t) => {
  const data = { result: [{ id: "system", name: "System", tools: ["cmake", "clang++", "git"] }] };
  const { root, content } = mount(t, toolsetPresentation, data);
  assert.equal(root.querySelector("h1").textContent, "toolsets_list");
  assert.match(content.textContent, /cmake, clang\+\+, git/);
  assert.ok(!content.textContent.includes('"cmake"'));
  assert.ok(!content.textContent.includes("version"));
  assert.equal(root.querySelectorAll(".fm-record").length, 1);
});

test("toolset details render all tools, null versions, paths and unknown metadata", (t) => {
  const data = { id: "system", name: "System", extra: { origin: "local" },
    tools: [{ name: "clang++", kind: "compiler", path: "/opt/llvm/bin/clang++", version: null }] };
  const { root, content } = mount(t, toolsetPresentation, data);
  assert.equal(root.querySelector("h1").textContent, "toolset_get");
  for (const value of ["System", "origin: local", "compiler", "/opt/llvm/bin/clang++", "null"]) {
    assert.ok(content.textContent.includes(value));
  }
});

test("local filters never truncate the source; JSON and Copy all include filtered records", async (t) => {
  const data = { running: 1, completed: 120, processes: Array.from({ length: 121 }, (_, index) => ({
    process_id: "p" + index, status: index ? "timed_out" : "running", return_code: null,
  })) };
  const { root, win, content } = mount(t, processPresentation, data);
  assert.equal(root.querySelectorAll(".fm-record").length, 121);
  const select = root.querySelector("select");
  select.value = "running";
  select.dispatchEvent(new win.Event("change"));
  assert.equal(root.querySelectorAll(".fm-record").length, 1);
  assert.match(root.querySelector("footer").textContent, /1 \/ 121 records · filter active/);
  let copied;
  Object.defineProperty(win.navigator, "clipboard", { value: { writeText: async (text) => { copied = text; } } });
  click(root, "Copy entire result");
  await tick();
  assert.equal(copied, JSON.stringify(data, null, 2));
  click(root, "[ JSON ]");
  assert.equal(content.querySelector("pre").textContent, JSON.stringify(data, null, 2));
  assert.ok(content.querySelector(".fm-json-key"));
  assert.ok(content.querySelector(".fm-json-string"));
  assert.ok(content.querySelector(".fm-number"));
  click(root, "[ Fields ]");
  select.value = "";
  select.dispatchEvent(new win.Event("change"));
  const search = root.querySelector("input");
  search.value = "p120";
  search.dispatchEvent(new win.Event("input"));
  assert.equal(root.querySelectorAll(".fm-record").length, 1);
  assert.match(content.textContent, /p120/);
  search.value = "";
  search.dispatchEvent(new win.Event("input"));
  assert.equal(root.querySelectorAll(".fm-record").length, 121);
  assert.equal(data.processes.length, 121);
});

test("JSON highlighting preserves strings and never interprets markup", (t) => {
  const data = { '<img src=x onerror=alert(1)>': '</script><script>evil()</script> "escaped"\n',
    values: [true, false, null, -12.5, 1e30], empty: "" };
  const { root, content } = mount(t, workspacePresentation, data);
  assert.equal(root.querySelectorAll("img, script").length, 0);
  click(root, "[ JSON ]");
  assert.equal(content.textContent, JSON.stringify(data, null, 2));
  assert.equal(root.querySelectorAll("img, script").length, 0);
  assert.equal(jsonTokens(data).map((token) => token.text).join(""), JSON.stringify(data, null, 2));
  assert.ok(content.querySelector(".fm-boolean"));
  assert.ok(content.querySelector(".fm-null"));
});

test("timestamp copy retains original precision and clipboard denial selects complete original text", async (t) => {
  const original = "2026-09-07T15:45:12.123456789+03:00";
  const { root, win, content } = mount(t, processPresentation, {
    processes: [{ started_at: original, status: "running" }],
  });
  Object.defineProperty(win.navigator, "clipboard", { value: { writeText: async () => { throw new Error("Denied"); } } });
  click(root, "Copy started_at");
  await tick();
  assert.equal(content.querySelector("pre").textContent, original);
  assert.equal(win.getSelection().toString(), original);
  assert.match(root.querySelector("footer").textContent, /Ctrl\+C/);
  click(root, "[ JSON ]");
  assert.ok(content.textContent.includes(original));
});

test("copy icons write complete original individual values", async (t) => {
  const data = { name: "test", tools: ["a", "b"], empty: "", nothing: null };
  const { root, win } = mount(t, workspacePresentation, data);
  const copies = [];
  Object.defineProperty(win.navigator, "clipboard", { value: { writeText: async (text) => { copies.push(text); } } });
  for (const key of Object.keys(data)) { click(root, "Copy " + key); await tick(); }
  assert.deepEqual(copies, ["test", JSON.stringify(data.tools, null, 2), "", "null"]);
});

test("new input, errors and teardown clear previous data and filters", async (t) => {
  const { root, view, win, content } = mount(t, toolsetPresentation, { result: [{ id: "old", name: "Old result", tools: ["oldtool"] }] });
  let finish;
  Object.defineProperty(win.navigator, "clipboard", { value: { writeText: () => new Promise((resolve) => { finish = resolve; }) } });
  click(root, "Copy entire result");
  view.pending();
  finish();
  await tick();
  assert.ok(!root.textContent.includes("Old result"));
  assert.ok(!root.textContent.includes("Copied"));
  assert.equal(root.querySelector(".fm-copy-all").disabled, true);
  view.receive({ isError: true, content: [{ type: "text", text: "Expected error <details>" }] });
  assert.ok(content.textContent.includes("Expected error <details>"));
  assert.equal(content.querySelector("details"), null);
  view.receive({ content: [{ type: "text", text: '{"name":"text only"}' }] });
  assert.match(content.textContent, /No structured result/);
  assert.equal(root.querySelectorAll("dt").length, 0);
  view.receive({ structuredContent: { id: "new", name: "New result", tools: [] } });
  assert.equal(root.querySelector("input").value, "");
  assert.equal(root.querySelector("h1").textContent, "toolset_get");
  assert.match(content.textContent, /No records in this result/);
  assert.ok(!content.textContent.includes("oldtool"));
  view.cancelled();
  assert.ok(!content.textContent.includes("New result"));
  assert.match(root.textContent, /Tool invocation cancelled/);
  view.dispose();
  view.receive({ structuredContent: { name: "late" } });
  assert.equal(root.childElementCount, 0);
});

test("timestamps use explicit UTC seconds; invalid dates remain unformatted", () => {
  assert.deepEqual(timestamp("2026-09-07T00:30:00.999-03:00"), {
    readable: "7 Sept 2026, 03:30:00 UTC", iso: "2026-09-07T03:30:00Z",
  });
  for (const value of [null, 42, "unknown", "2026-09-07T00:30:00", "2026-99-99T00:00:00Z"]) {
    assert.equal(timestamp(value), null);
  }
});

test("semantic colors distinguish failure, success, progress and cancellation", () => {
  assert.equal(statusTone("timed_out"), "fm-error");
  assert.equal(statusTone("failed"), "fm-error");
  assert.equal(statusTone("exited", { return_code: 1 }), "fm-error");
  assert.equal(statusTone("exited", { return_code: 0 }), "fm-success");
  assert.equal(statusTone("exited", { return_code: null }), "");
  assert.equal(statusTone("running"), "fm-active");
  assert.equal(statusTone("terminated"), "fm-warning");
});
