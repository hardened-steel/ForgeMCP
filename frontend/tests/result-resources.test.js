import test from "node:test";
import assert from "node:assert/strict";
import { loadResultDiff } from "../src/shared/result-resources.js";

const base = "forgemcp://workspace/results/abc/";
function fixture() {
  const link = { uri: base + "diff.json", mime_type: "application/json" };
  const result = { structuredContent: { path: "project/a.cpp", replacements: 1, resources: { diff: link } } };
  const diff = {
    path: "project/a.cpp",
    changes: [{ kind: "insert", before_start: 1, before_count: 0, after_start: 1, after_count: 1 }],
    hunks: [{ before_start: 1, before_count: 0, after_start: 1, after_count: 1, lines: [
      { kind: "added", before_line: null, after_line: 1, text: "<script>λ</script>\n" },
    ] }],
  };
  const calls = [];
  const read = async ({ uri }) => {
    calls.push(uri);
    return { contents: [{ uri, mimeType: "application/json", text: JSON.stringify(diff) }] };
  };
  return { result, diff, read, calls };
}

test("reads the named typed diff directly without a manifest", async () => {
  const f = fixture();
  assert.deepEqual(await loadResultDiff(f.result, f.read), f.diff);
  assert.deepEqual(f.calls, [base + "diff.json"]);
});

test("rejects foreign links, traversal, wrong MIME types, and malformed diffs", async () => {
  for (const mutate of [
    (f) => { f.result.structuredContent.resources.diff.uri = "https://example.com/diff.json"; },
    (f) => { f.result.structuredContent.resources.diff.uri = base + "../diff.json"; },
    (f) => { f.result.structuredContent.resources.diff.mime_type = "text/plain"; },
    (f) => { f.diff.path = "project/other.cpp"; },
    (f) => { f.diff.hunks[0].lines[0].after_line = -1; },
    (f) => { f.diff.hunks[0].lines[0].spans = [[0, 900]]; },
  ]) {
    const f = fixture();
    mutate(f);
    await assert.rejects(loadResultDiff(f.result, f.read));
    assert.ok(f.calls.every((uri) => uri.startsWith(base)));
  }
});

test("ignores stale loads and unknown resource versions", async () => {
  const f = fixture();
  assert.equal(await loadResultDiff(f.result, f.read, () => false), null);
  assert.equal(f.calls.length, 0);
  f.result.structuredContent.resources.diff.version = 2;
  assert.equal(await loadResultDiff(f.result, f.read), null);
  assert.equal(f.calls.length, 0);
  const stale = fixture();
  assert.equal(await loadResultDiff(stale.result, stale.read, () => stale.calls.length === 0), null);
  assert.equal(stale.calls.length, 1);
});

test("host failures remain resource-loading errors", async () => {
  const f = fixture();
  await assert.rejects(loadResultDiff(f.result, async () => { throw new Error("Host denied resources/read"); }));
  assert.equal(await loadResultDiff({ structuredContent: { path: "project/a.cpp" } }, f.read), null);
});
