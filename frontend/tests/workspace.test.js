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

test("tree distinguishes empty and unexpanded directories without null children", (t) => {
  const data = { root: "project", path: ".", entries: [
    { path: "src", kind: "directory", children: [{ path: "src/<script>.cpp", kind: "file", children: null, extra: "visible" }] },
    { path: "empty", kind: "directory", children: [] },
    { path: "unexpanded", kind: "directory", children: null },
    { path: "alias", kind: "symlink", children: null },
  ] };
  const { root } = mount(t, data);
  assert.equal(root.querySelector("h1").textContent, "workspace_list");
  for (const value of ["src/<script>.cpp", "empty directory", "visible", "symlink"]) assert.ok(root.textContent.includes(value));
  assert.equal(root.querySelector("script"), null);
  assert.ok(!root.textContent.includes("children:"));
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

test("search groups and collapses files; skipped files have their own tab", (t) => {
  const data = { root: "project", skipped_files: ["data.bin", "legacy.txt"], matches: [
    { path: "a.cpp", line: 15, text: "alpha <tag>", spans: [[0, 5]] },
    { path: "a.cpp", line: 16, text: "alpha again", spans: [[0, 5]] },
    { path: "b.cpp", line: 201, text: "beta", spans: [[0, 4]] },
  ] };
  const { root, win } = mount(t, data);
  assert.equal(root.querySelectorAll(".fm-search-file").length, 2);
  assert.equal(root.querySelectorAll("mark").length, 3);
  assert.equal(root.querySelector(".fm-search-skipped").hidden, true);
  assert.equal(root.querySelectorAll(".fm-match-line button").length, 0);
  const file = root.querySelector(".fm-search-file");
  file.querySelector("summary").click();
  assert.equal(file.open, false);
  root.querySelectorAll('[role="tab"]')[1].click();
  assert.equal(root.querySelector(".fm-search-matches").hidden, true);
  assert.equal(root.querySelector(".fm-search-skipped").hidden, false);
  assert.ok(root.querySelector(".fm-search-skipped").textContent.includes("data.bin"));
  const search = root.querySelector("input");
  search.value = "beta";
  search.dispatchEvent(new win.Event("input"));
  assert.equal(root.querySelectorAll(".fm-search-file").length, 1);
  root.querySelectorAll(".fm-views button")[1].click();
  assert.equal(root.querySelector("pre").textContent, JSON.stringify(data, null, 2));
});

test("long matches clip around server spans and only clipped lines expand", (t) => {
  const text = "😀".repeat(180) + "<needle>" + "z".repeat(180);
  const { root } = mount(t, { root: "project", skipped_files: [], matches: [
    { path: "x.cpp", line: 2, text, spans: [[180, 188]] },
    { path: "x.cpp", line: 3, text: "short", spans: [[0, 5]] },
    { path: "x.cpp", line: 4, text: "needle" + "z".repeat(200), spans: [[0, 6]] },
    { path: "x.cpp", line: 5, text: "z".repeat(200) + "needle", spans: [[200, 206]] },
  ] });
  const rows = root.querySelectorAll(".fm-match-line");
  assert.equal(rows[0].querySelector("mark").textContent, "<needle>");
  assert.ok(rows[0].textContent.startsWith("…") && rows[0].textContent.endsWith("…"));
  assert.equal(rows[1].tagName, "DIV");
  assert.ok(!rows[2].textContent.startsWith("…") && rows[2].textContent.endsWith("…"));
  assert.ok(rows[3].textContent.startsWith("…") && !rows[3].textContent.endsWith("…"));
  rows[0].click();
  assert.equal(rows[0].textContent, text);
  assert.equal(rows[0].getAttribute("aria-expanded"), "true");
  rows[0].click();
  assert.equal(rows[0].getAttribute("aria-expanded"), "false");
  assert.equal(root.querySelector("needle"), null);
});

test("file wrap toggle selects horizontal scrolling without changing text", (t) => {
  const text = "very long ".repeat(100);
  const { root } = mount(t, { root: "project", path: "a.cpp", text, start_line: 1 });
  const toggle = root.querySelector(".fm-source button");
  assert.equal(toggle.getAttribute("aria-pressed"), "true");
  toggle.click();
  assert.ok(root.querySelector(".fm-source-nowrap .fm-source-viewport"));
  assert.equal(root.querySelector("code").textContent, text);
  toggle.click();
  assert.equal(root.querySelector(".fm-source-nowrap"), null);
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
