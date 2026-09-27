import test from "node:test";
import assert from "node:assert/strict";
import { loadResultDiff } from "../src/shared/result-resources.js";

const base = "forgemcp://workspace/results/abc/";
function fixture() {
  const link = { uri: base + "diff.json", mime_type: "application/json" };
  const result = { structuredContent: { path: "project/a.cpp", replacements: 1, extensions_uri: base + "extensions.json", resources: [link] } };
  const manifest = { extensions: [{ kind: "diff", version: 1, data: { resource: "diff.json" }, error: null }], resources: [link] };
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
    return { contents: [{ uri, mimeType: "application/json", text: JSON.stringify(uri.endsWith("extensions.json") ? manifest : diff) }] };
  };
  return { result, manifest, diff, read, calls };
}

test("reads manifest and only its explicitly linked typed diff", async () => {
  const f = fixture();
  assert.deepEqual(await loadResultDiff(f.result, f.read), f.diff);
  assert.deepEqual(f.calls, [base + "extensions.json", base + "diff.json"]);
});

test("rejects foreign manifests, unlinked resources, traversal, and malformed diffs", async () => {
  for (const mutate of [
    (f) => { f.result.structuredContent.extensions_uri = "https://example.com/extensions.json"; },
    (f) => { f.result.structuredContent.resources = []; },
    (f) => { f.manifest.resources = []; },
    (f) => { f.manifest.extensions[0].data.resource = "../diff.json"; },
    (f) => { f.diff.path = "project/other.cpp"; },
    (f) => { f.diff.hunks[0].lines[0].after_line = -1; },
  ]) {
    const f = fixture();
    mutate(f);
    await assert.rejects(loadResultDiff(f.result, f.read));
    assert.ok(f.calls.every((uri) => uri.startsWith(base)));
  }
});

test("ignores stale loads and unknown extension versions", async () => {
  const f = fixture();
  assert.equal(await loadResultDiff(f.result, f.read, () => false), null);
  assert.equal(f.calls.length, 1);
  f.manifest.extensions[0].version = 2;
  assert.equal(await loadResultDiff(f.result, f.read), null);
  assert.equal(f.calls.length, 2);
  const stale = fixture();
  assert.equal(await loadResultDiff(stale.result, stale.read, () => stale.calls.length < 2), null);
  assert.equal(stale.calls.length, 2);
});

test("provider and host failures remain resource-loading errors", async () => {
  const f = fixture();
  f.manifest.extensions[0].error = "Extension provider failed.";
  await assert.rejects(loadResultDiff(f.result, f.read));
  await assert.rejects(loadResultDiff(f.result, async () => { throw new Error("Host denied resources/read"); }));
  assert.equal(await loadResultDiff({ structuredContent: { path: "project/a.cpp" } }, f.read), null);
});
