import test from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import { createResultView } from "../src/shared/result-view.js";
import { workspacePresentation, workspaceValue } from "../src/workspace-view.js";

function mount(t, data) {
  const dom = new JSDOM("<main></main>");
  t.after(() => dom.window.close());
  const root = dom.window.document.querySelector("main");
  const view = createResultView(root, { toolName: "workspace", describe: workspacePresentation, renderValue: workspaceValue });
  view.receive({ structuredContent: data });
  return { root, view, win: dom.window };
}

test("tree displays every depth, empty/null children, links, and future fields", (t) => {
  const data = { root: "project", path: ".", entries: [
    { path: "src", kind: "directory", children: [{ path: "src/<script>.cpp", kind: "file", children: null, extra: "visible" }] },
    { path: "empty", kind: "directory", children: [] },
    { path: "unexpanded", kind: "directory", children: null },
    { path: "alias", kind: "symlink", children: null },
  ] };
  const { root } = mount(t, data);
  assert.equal(root.querySelector("h1").textContent, "workspace_list");
  for (const value of ["src/<script>.cpp", "Empty list", "null", "visible", "symlink"]) assert.ok(root.textContent.includes(value));
  assert.equal(root.querySelector("script"), null);
  assert.equal(root.querySelectorAll(".fm-tree-path").length, 5);
  root.querySelectorAll(".fm-views button")[1].click();
  assert.equal(root.querySelector("pre").textContent, JSON.stringify(data, null, 2));
});

test("file text retains all lines, Unicode, long values and its original copy", async (t) => {
  const text = "<script>λ</script>\r\n" + "long".repeat(300) + "\nlast";
  const data = { root: "storage", path: "build/log.txt", text, start_line: 41, future: "present" };
  const { root, win, view } = mount(t, data);
  assert.equal(root.querySelector("h1").textContent, "workspace_read_file");
  assert.deepEqual([...root.querySelectorAll(".fm-source-line")].map((node) => node.dataset.line), ["41", "42", "43"]);
  assert.equal([...root.querySelectorAll("code")].map((node) => node.textContent).join(""), text);
  assert.equal(root.querySelector("script"), null);
  assert.ok(root.textContent.includes("present"));
  let copied;
  Object.defineProperty(win.navigator, "clipboard", { value: { writeText: async (text) => { copied = text; } } });
  root.querySelector('[aria-label="Copy text"]').click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(copied, text);
  view.receive({ structuredContent: { root: "project", path: "empty", text: "", start_line: 1 } });
  assert.equal(root.querySelectorAll(".fm-source-line").length, 0);
  assert.ok(root.textContent.includes("Empty string"));
  view.pending();
  assert.equal(root.querySelector(".fm-source"), null);
});

test("search shows skipped files and matches, filtering preserves JSON and copying", async (t) => {
  const data = { root: "project", skipped_files: ["data.bin", "legacy.txt"], matches: [
    { path: "a.cpp", line: 15, text: "alpha <tag>" },
    { path: "b.cpp", line: 201, text: "beta" },
  ] };
  const { root, win } = mount(t, data);
  assert.equal(root.querySelector("h1").textContent, "workspace_search");
  assert.ok(root.textContent.includes("data.bin, legacy.txt"));
  assert.deepEqual([...root.querySelectorAll(".fm-source-line")].map((node) => node.dataset.line), ["15", "201"]);
  const search = root.querySelector("input");
  search.value = "beta";
  search.dispatchEvent(new win.Event("input"));
  assert.equal(root.querySelectorAll(".fm-record").length, 1);
  assert.match(root.querySelector("footer").textContent, /1 \/ 2 records/);
  root.querySelectorAll(".fm-views button")[1].click();
  assert.equal(root.querySelector("pre").textContent, JSON.stringify(data, null, 2));
});

test("find results expose every path with local filtering", (t) => {
  const data = { root: "project", paths: Array.from({ length: 150 }, (_, i) => `src/file-${i}.cpp`) };
  const { root, win } = mount(t, data);
  assert.equal(root.querySelectorAll(".fm-record").length, 150);
  root.querySelector("input").value = "file-149";
  root.querySelector("input").dispatchEvent(new win.Event("input"));
  assert.equal(root.querySelectorAll(".fm-record").length, 1);
  assert.ok(root.textContent.includes("src/file-149.cpp"));
});

test("mutation and metadata shapes select the correct tool without additional calls", (t) => {
  const cases = [
    ["workspace_write_file", { action: "created", lines_added: 30, lines_removed: 0 }],
    ["workspace_edit_file", { replacements: 4 }],
    ["workspace_move", { action: "moved", source: "old" }],
    ["workspace_delete", { action: "deleted" }],
    ["workspace_mkdir", { action: "already_exists" }],
    ["workspace_file_info", { size_bytes: 20, modified_at: "2026-09-19T10:00:00Z", owner: null, created_at: null }],
  ];
  for (const [name, fields] of cases) {
    const data = { root: "project", path: "example", ...fields };
    const { root, view } = mount(t, data);
    assert.equal(root.querySelector("h1").textContent, name);
    assert.equal(root.querySelectorAll("dt").length, Object.keys(data).length);
    view.receive({ isError: true, content: [{ type: "text", text: "Expected one occurrence" }] });
    assert.ok(root.textContent.includes("Expected one occurrence"));
    assert.equal(root.querySelector("dl"), null);
  }
});
