/** Readable, local-only projections of one immutable Git result. */
import { isObject } from "./shared/presentation.js";
import { createCopyIcon } from "./shared/copy-icon.js";

const STATUS = {
  ".": ["Unchanged", "muted"], " ": ["Unchanged", "muted"],
  M: ["Modified", "warning"], A: ["Added", "success"], D: ["Deleted", "error"],
  R: ["Renamed", "active"], C: ["Copied", "active"], U: ["Unmerged", "error"],
  T: ["Type changed", "warning"], "?": ["Untracked", "muted"], "!": ["Ignored", "muted"],
};
const operationName = (value) => String(value ?? "").replaceAll("_", "-");
const shortOid = (value) => typeof value === "string" ? value.slice(0, 8) : "No commit";
const matches = (value, query) => !query || JSON.stringify(value).toLocaleLowerCase().includes(query);
const UNMERGED = new Set(["DD", "AU", "UD", "UA", "DU", "AA", "UU"]);
const isConflict = (entry) => UNMERGED.has(`${entry.index_status}${entry.worktree_status}`);

/** Create literal DOM nodes; repository data never becomes markup or a link. */
function node(doc, tag, className = "", text) {
  const element = doc.createElement(tag);
  element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}

/** Keep date display compact while preserving the exact input in the title and details. */
function dateText(value) {
  if (typeof value !== "string" || Number.isNaN(Date.parse(value))) return String(value ?? "");
  return `${new Intl.DateTimeFormat("en-GB", {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
    timeZone: "UTC", hourCycle: "h23",
  }).format(new Date(value))} UTC`;
}

/** Create an accessible local clipboard action using the shared fallback policy. */
function copyButton(doc, value, label, tools) {
  if (!tools.copy) return null;
  const button = node(doc, "button", "fm-copy fm-git-copy");
  button.type = "button";
  button.setAttribute("aria-label", label);
  button.append(createCopyIcon(doc));
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    void tools.copy(typeof value === "string" ? value : JSON.stringify(value, null, 2));
  });
  tools.help?.(button, label);
  return button;
}

/** Display a full path with an optional rename and a copy action for its exact value. */
function pathLabel(doc, record, tools) {
  const group = node(doc, "span", "fm-git-path-group");
  const text = node(doc, "span", "fm-git-path-text");
  if (record.original_path && record.original_path !== record.path) {
    text.append(node(doc, "span", "fm-git-old-path", `${record.original_path} → `));
  }
  text.append(node(doc, "span", "fm-git-path", record.path));
  group.append(text);
  const copy = copyButton(doc, record.path, "Copy full path", tools);
  if (copy) group.append(copy);
  return group;
}

/** Present status with both a readable word and a theme-aware tone. */
function badge(doc, status, override) {
  const [label, tone] = STATUS[String(status).charAt(0)] ?? [String(status), "muted"];
  const value = node(doc, "span", `fm-git-badge fm-git-${tone}`, override ?? label);
  value.title = `Git status: ${status}`;
  return value;
}

/** Preserve labeled original data in a collapsed Fields panel, including unknown fields. */
function detailsFields(doc, title, data, tools) {
  const details = node(doc, "details", "fm-git-details");
  details.append(node(doc, "summary", "", title));
  let populated = false;
  details.addEventListener("toggle", () => {
    if (!details.open || populated) return;
    populated = true;
    if (tools.fields) details.append(tools.fields(data));
    else details.append(node(doc, "pre", "fm-git-literal", JSON.stringify(data, null, 2)));
  });
  return details;
}

/** Make result counts and active filtering visible beside the data they describe. */
function resultCount(doc, shown, total, noun, query) {
  const label = total === 1 ? noun === "stashes" ? "stash" : noun.replace(/s$/, "") : noun;
  return node(doc, "p", "fm-git-count", query ? `${shown} / ${total} ${label} match` : `${total} ${label}`);
}

/** Display a readable state or warning without depending on color alone. */
function notice(doc, title, text, tone = "muted") {
  const box = node(doc, "section", `fm-git-notice fm-git-${tone}`);
  box.append(node(doc, "h2", "", title));
  if (text) box.append(node(doc, "p", "", text));
  return box;
}

/** Render independent staged and working-tree states as compact file rows. */
function changeRows(doc, entries, query, tools, title = "Changed paths") {
  const section = node(doc, "section", "fm-git-changes");
  section.append(node(doc, "h2", "fm-git-section-title", title));
  const shown = entries.filter((entry) => matches({
    ...entry,
    staged: entry.index_status === "?" ? "Not staged" : STATUS[entry.index_status]?.[0],
    working_tree: STATUS[entry.worktree_status]?.[0],
    conflict: isConflict(entry) ? "Conflict" : undefined,
  }, query));
  section.append(resultCount(doc, shown.length, entries.length, "paths", query));
  if (!shown.length) {
    section.append(node(doc, "p", "fm-git-empty", entries.length ? "No paths match this filter." : "No changed paths."));
    return section;
  }
  const header = node(doc, "div", "fm-git-change-header");
  header.append(node(doc, "span", "", "Path"), node(doc, "span", "", "Staged"), node(doc, "span", "", "Working tree"));
  section.append(header);
  for (const entry of shown) {
    const row = node(doc, "div", "fm-git-change-row");
    const path = pathLabel(doc, entry, tools);
    if (isConflict(entry)) path.querySelector(".fm-git-path-text").prepend(badge(doc, "U", "Conflict"), doc.createTextNode(" "));
    row.append(
      path,
      badge(doc, entry.index_status, entry.index_status === "?" ? "Not staged" : undefined),
      badge(doc, entry.worktree_status),
    );
    if (entry.submodule && entry.submodule !== "N...") {
      const state = entry.submodule;
      const changes = /^S[.C][.M][.U]$/.test(state) ? [
        state[1] === "C" ? "commit changed" : null,
        state[2] === "M" ? "modified files" : null,
        state[3] === "U" ? "untracked files" : null,
      ].filter(Boolean).join(" · ") || "no content changes" : state;
      row.append(node(doc, "span", "fm-git-submodule", `Submodule · ${changes}`));
    }
    section.append(row);
  }
  return section;
}

/** Summarize the checked-out branch without hiding detached or initial states. */
function statusView(doc, data, query, tools) {
  const result = node(doc, "div");
  const heading = node(doc, "div", "fm-git-repository");
  heading.append(node(doc, "strong", "", data.branch ?? (data.head ? "Detached HEAD" : "No commits yet")));
  if (data.head) heading.append(node(doc, "code", "fm-git-oid", shortOid(data.head)));
  else if (data.branch) heading.append(node(doc, "span", "fm-git-muted", "No commits yet"));
  result.append(heading);
  if (data.upstream) {
    const tracking = [`Tracking ${data.upstream}`];
    if (data.ahead !== null && data.ahead !== undefined) tracking.push(`${data.ahead} ahead`);
    if (data.behind !== null && data.behind !== undefined) tracking.push(`${data.behind} behind`);
    result.append(node(doc, "p", "fm-git-context", tracking.join(" · ")));
  }
  if (data.operation) result.append(notice(doc, `${operationName(data.operation)} in progress`, "Inspect conflicts or continue/abort the current operation with the Git tools.", "warning"));
  const entries = data.entries ?? [];
  if (!entries.length) result.append(notice(doc, "Working tree clean", "No staged, unstaged or untracked paths.", "success"));
  else result.append(changeRows(doc, entries, query, tools));
  return result;
}

/** Display a subject-first commit card with complete metadata available on expansion. */
function commitCard(doc, commit, tools, open = false) {
  const card = node(doc, "details", "fm-git-commit");
  card.open = open;
  const heading = node(doc, "summary", "");
  heading.append(node(doc, "span", "fm-git-subject", String(commit.message ?? "").split("\n")[0] || "Empty commit message"));
  const meta = node(doc, "span", "fm-git-commit-meta");
  meta.append(node(doc, "code", "fm-git-oid", shortOid(commit.oid)));
  const copy = copyButton(doc, commit.oid, "Copy full commit ID", tools);
  if (copy) meta.append(copy);
  meta.append(node(doc, "span", "", commit.author_name ?? "Unknown author"));
  const time = node(doc, "time", "", dateText(commit.author_time));
  if (commit.author_time) time.dateTime = time.title = commit.author_time;
  meta.append(time);
  heading.append(meta);
  card.append(heading);
  card.append(node(doc, "pre", "fm-git-message", commit.message ?? ""));
  card.append(detailsFields(doc, "Commit details", commit, tools));
  return card;
}

/** Show one bounded history page without making additional Git calls. */
function historyView(doc, data, query, tools) {
  const result = node(doc, "div");
  const commits = data.commits ?? [];
  const shown = commits.filter((commit) => matches(commit, query));
  result.append(resultCount(doc, shown.length, commits.length, "commits", query));
  for (const commit of shown) result.append(commitCard(doc, commit, tools));
  if (!shown.length) result.append(node(doc, "p", "fm-git-empty", commits.length ? "No commits match this filter." : "No commits in this result."));
  if (data.next_skip !== null && data.next_skip !== undefined) result.append(node(doc, "p", "fm-git-context", `More history is available. Next git_log skip: ${data.next_skip}.`));
  return result;
}

/** Render original unified patch lines with old/new line numbers where Git supplies a hunk. */
function patchTable(doc, lines) {
  const table = node(doc, "table", "fm-git-code");
  table.setAttribute("aria-label", "Unified patch");
  const head = node(doc, "thead");
  const labels = node(doc, "tr");
  for (const label of ["Old", "New", "Patch"]) {
    const cell = node(doc, "th", "", label);
    cell.scope = "col";
    labels.append(cell);
  }
  head.append(labels);
  table.append(head);
  const body = node(doc, "tbody");
  let oldLine = null;
  let newLine = null;
  for (const text of lines) {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text);
    let old = "";
    let current = "";
    let tone = "meta";
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      tone = "hunk";
    } else if (oldLine !== null && (text.startsWith("+") || text.startsWith("-") || text.startsWith(" "))) {
      if (!text.startsWith("+")) old = oldLine++;
      if (!text.startsWith("-")) current = newLine++;
      tone = text.startsWith("+") ? "add" : text.startsWith("-") ? "remove" : "context";
    }
    const row = node(doc, "tr", `fm-git-patch-${tone}`);
    row.append(node(doc, "td", "fm-git-line-number", old), node(doc, "td", "fm-git-line-number", current));
    const content = node(doc, "td", "fm-git-code-text");
    content.append(node(doc, "code", "", text || "\n"));
    row.append(content);
    body.append(row);
  }
  table.append(body);
  return table;
}

/** Separate patch files at Git's own headers while retaining every original line. */
function patchBlocks(patch) {
  const blocks = [];
  for (const line of String(patch ?? "").split("\n")) {
    if (line.startsWith("diff --git ") || !blocks.length) blocks.push([]);
    blocks.at(-1).push(line);
  }
  return blocks;
}

/** Put the patch before detailed object IDs, with local wrap and copy controls. */
function diffView(doc, data, query, tools) {
  const result = node(doc, "section", "fm-git-diff");
  const files = data.files ?? [];
  const labels = { staged: "Staged changes", unstaged: "Unstaged changes", revision: "Revision comparison" };
  const heading = node(doc, "div", "fm-git-diff-heading");
  heading.append(node(doc, "h2", "fm-git-section-title", labels[data.mode] ?? "Changes"));
  const wrap = node(doc, "button", "fm-git-wrap", "Wrap lines");
  wrap.type = "button";
  wrap.setAttribute("aria-pressed", "false");
  wrap.addEventListener("click", () => {
    const active = wrap.getAttribute("aria-pressed") !== "true";
    wrap.setAttribute("aria-pressed", String(active));
    result.classList.toggle("fm-git-wrapped", active);
  });
  heading.append(wrap);
  const copy = copyButton(doc, data.patch ?? "", "Copy complete original patch", tools);
  if (copy) heading.append(copy);
  result.append(heading);
  if (data.base || data.target) result.append(node(doc, "p", "fm-git-context", `${data.base ?? "Index"} → ${data.target ?? "Working tree"}`));
  if (data.had_decoding_errors) result.append(notice(doc, "Some bytes could not be decoded", "The displayed patch contains replacement characters.", "warning"));
  if (!data.patch) {
    result.append(node(doc, "p", "fm-git-empty", files.length ? "No textual patch was returned. Inspect file details below." : "No differences in this comparison."));
    return result;
  }
  const blocks = patchBlocks(data.patch);
  const aligned = blocks.length === files.length && blocks.every((block) => block[0]?.startsWith("diff --git "));
  let shown = 0;
  for (const [index, lines] of blocks.entries()) {
    const file = aligned ? files[index] : null;
    if (!matches({ file, patch: lines.join("\n") }, query)) continue;
    shown++;
    const section = node(doc, "details", "fm-git-patch-file");
    section.open = true;
    const title = node(doc, "summary");
    if (file) title.append(badge(doc, file.status), pathLabel(doc, file, tools));
    else title.append(node(doc, "span", "fm-git-path", lines[0]?.startsWith("diff --git ") ? lines[0].slice(11) : "Patch"));
    section.append(title);
    const viewport = node(doc, "div", "fm-git-code-viewport");
    viewport.tabIndex = 0;
    viewport.setAttribute("aria-label", `Patch for ${file?.path ?? "comparison"}; scroll horizontally to read long lines`);
    const hunk = lines.findIndex((line) => /^@@ /.test(line));
    const headers = hunk >= 0 ? lines.slice(0, hunk) : lines;
    if (hunk >= 0) {
      viewport.append(patchTable(doc, lines.slice(hunk)));
      section.append(viewport);
    } else {
      section.append(node(doc, "p", "fm-git-empty", lines.some((line) => /^(Binary files |GIT binary patch)/.test(line))
        ? "Binary files differ. See original patch headers below."
        : "No text hunks in this file. See original patch output below."));
    }
    if (headers.length) {
      const metadata = node(doc, "details", "fm-git-patch-headers");
      metadata.append(node(doc, "summary", "", "Original patch headers"));
      metadata.append(node(doc, "pre", "fm-git-literal", headers.join("\n")));
      section.append(metadata);
    }
    result.append(section);
  }
  result.prepend(resultCount(doc, shown, blocks.length, "patch files", query));
  if (!shown) result.append(node(doc, "p", "fm-git-empty", "No patch files match this filter."));
  return result;
}

/** Render compact refs with current-branch and upstream information. */
function refsView(doc, data, query, tools) {
  const result = node(doc, "div");
  const refs = data.refs ?? [];
  const shown = refs.filter((ref) => matches(ref, query));
  result.append(resultCount(doc, shown.length, refs.length, "refs", query));
  for (const ref of shown) {
    const row = node(doc, "div", "fm-git-ref");
    row.append(node(doc, "strong", "fm-git-path", ref.name));
    if (ref.current) row.append(node(doc, "span", "fm-git-badge fm-git-success", "Current"));
    row.append(node(doc, "code", "fm-git-oid", shortOid(ref.oid)));
    const copy = copyButton(doc, ref.oid, "Copy full object ID", tools);
    if (copy) row.append(copy);
    if (ref.upstream) row.append(node(doc, "span", "fm-git-upstream", `Tracking ${ref.upstream}`));
    result.append(row);
  }
  if (!shown.length) result.append(node(doc, "p", "fm-git-empty", refs.length ? "No refs match this filter." : "No refs in this result."));
  return result;
}

/** Group remote addresses under their fetch and push purposes without making links. */
function remotesView(doc, data, query) {
  const result = node(doc, "div");
  const remotes = data.remotes ?? [];
  const shown = remotes.filter((remote) => matches(remote, query));
  result.append(resultCount(doc, shown.length, remotes.length, "remotes", query));
  for (const remote of shown) {
    const section = node(doc, "section", "fm-git-remote");
    section.append(node(doc, "h2", "fm-git-section-title", remote.name));
    for (const [label, urls] of [["Fetch", remote.fetch_urls], ["Push", remote.push_urls]]) {
      const line = node(doc, "div", "fm-git-remote-line");
      line.append(node(doc, "span", "fm-git-muted", label));
      const values = node(doc, "div");
      for (const url of urls ?? []) values.append(node(doc, "div", "fm-git-address", url));
      if (!urls?.length) values.append(node(doc, "span", "fm-git-muted", "No address"));
      line.append(values);
      section.append(line);
    }
    result.append(section);
  }
  if (!shown.length) result.append(node(doc, "p", "fm-git-empty", remotes.length ? "No remotes match this filter." : "No configured remotes."));
  return result;
}

/** Present stash selectors alongside messages rather than nested raw object fields. */
function stashesView(doc, stashes, query, tools) {
  const result = node(doc, "section");
  result.append(node(doc, "h2", "fm-git-section-title", "Stashes"));
  const shown = stashes.filter((stash) => matches(stash, query));
  result.append(resultCount(doc, shown.length, stashes.length, "stashes", query));
  for (const stash of shown) {
    const card = node(doc, "details", "fm-git-commit");
    const summary = node(doc, "summary");
    summary.append(
      node(doc, "code", "fm-git-oid fm-git-stash-reference", stash.reference),
      doc.createTextNode(" · "),
      node(doc, "span", "fm-git-subject", stash.message),
    );
    card.append(summary, detailsFields(doc, "Stash details", stash, tools));
    result.append(card);
  }
  if (!shown.length) result.append(node(doc, "p", "fm-git-empty", stashes.length ? "No stashes match this filter." : "No stashes in this result."));
  return result;
}

/** Put Git outcomes and conflicts before the actual post-command worktree snapshot. */
function mutationView(doc, data, query, tools) {
  const result = node(doc, "div");
  if (data.error) result.append(notice(doc, "Git command needs attention", data.error, "error"));
  else if (data.conflicts?.length) result.append(notice(doc, "Conflicts need resolution", `${data.conflicts.length} unresolved paths in the index.`, "error"));
  else if (data.operation) result.append(notice(doc, `${operationName(data.operation)} in progress`, "The operation has not finished.", "warning"));
  else result.append(notice(doc, "Git command completed", null, "success"));
  if (data.synchronization_error) result.append(notice(doc, "Analysis synchronization needs attention", data.synchronization_error, "warning"));
  if (data.operation && (data.error || data.conflicts?.length)) result.append(node(doc, "p", "fm-git-context", `${operationName(data.operation)} is still in progress.`));
  if (data.conflicts?.length) {
    const section = node(doc, "section", "fm-git-conflict-paths");
    section.append(node(doc, "h2", "fm-git-section-title", "Unresolved conflicts"));
    const paths = data.conflicts.filter((path) => matches(path, query));
    section.append(resultCount(doc, paths.length, data.conflicts.length, "paths", query));
    for (const path of paths) section.append(pathLabel(doc, { path }, tools));
    if (!paths.length) section.append(node(doc, "p", "fm-git-empty", "No conflict paths match this filter."));
    result.append(section);
  }
  result.append(node(doc, "p", "fm-git-context", `HEAD ${shortOid(data.head_before)} → ${shortOid(data.head_after)}`));
  for (const update of data.updates ?? []) result.append(node(doc, "p", "fm-git-update", update));
  if (Array.isArray(data.stashes)) result.append(stashesView(doc, data.stashes, query, tools));
  result.append(changeRows(doc, data.changes ?? [], query, tools, "Current changes"));
  return result;
}

/** Render committed source lines with compact attribution and full per-line details. */
function blameView(doc, data, query, tools) {
  const result = node(doc, "section", "fm-git-blame");
  result.append(pathLabel(doc, data, tools));
  const lines = data.lines ?? [];
  const parentMatches = query && matches({ path: data.path, revision: data.revision }, query);
  const shown = lines.filter((line) => parentMatches || matches(line, query));
  result.append(resultCount(doc, shown.length, lines.length, "lines", query));
  for (const line of shown) {
    const row = node(doc, "details", "fm-git-blame-line");
    const summary = node(doc, "summary");
    const attribution = node(doc, "span", "fm-git-attribution");
    attribution.append(node(doc, "code", "fm-git-oid", shortOid(line.oid)), node(doc, "span", "", line.author));
    const number = node(doc, "span", "fm-git-line-number", line.line);
    number.setAttribute("aria-label", `Line ${line.line}`);
    summary.append(attribution, number, node(doc, "code", "fm-git-blame-text", line.text || " "));
    row.append(summary, detailsFields(doc, "Line attribution", line, tools));
    result.append(row);
  }
  if (!shown.length) result.append(node(doc, "p", "fm-git-empty", lines.length ? "No lines match this filter." : "No source lines in this result."));
  return result;
}

/** Keep absent, binary, and text conflict stages visually distinct and explicitly named. */
function conflictsView(doc, data, query, tools) {
  const result = node(doc, "div");
  if (data.operation) result.append(notice(doc, `${operationName(data.operation)} in progress`, "These are the captured index stages; resolving a file requires a Git tool.", "warning"));
  if (data.operation === "rebase") result.append(node(doc, "p", "fm-git-context", "During rebase, ours is the rebased state and theirs is the commit being replayed."));
  const files = data.files ?? [];
  const shown = files.filter((file) => matches(file, query));
  result.append(resultCount(doc, shown.length, files.length, "conflicted files", query));
  for (const file of shown) {
    const card = node(doc, "details", "fm-git-conflict");
    card.open = true;
    const summary = node(doc, "summary");
    summary.append(pathLabel(doc, file, tools));
    card.append(summary);
    const stages = node(doc, "div", "fm-git-stages");
    for (const [key, title] of [["base", "Base · stage 1"], ["ours", "Ours · stage 2"], ["theirs", "Theirs · stage 3"]]) {
      const stage = node(doc, "details", "fm-git-stage");
      const blob = file[key];
      const heading = node(doc, "summary", "", title);
      heading.append(node(doc, "span", "fm-git-stage-kind", !blob ? "Absent" : blob.binary ? "Binary" : blob.text === null ? "Metadata only" : "Text"));
      stage.append(heading);
      if (!blob) stage.append(node(doc, "p", "fm-git-empty", "This stage has no file."));
      else {
        if (blob.text !== null && blob.text !== undefined) stage.append(node(doc, "pre", "fm-git-stage-text", blob.text || "Empty text file"));
        else stage.append(node(doc, "p", "fm-git-empty", blob.binary ? "Binary content is not displayed." : "Text content is not available for this stage."));
        stage.append(detailsFields(doc, "Stage details", blob, tools));
      }
      stages.append(stage);
    }
    card.append(stages);
    result.append(card);
  }
  if (!shown.length) result.append(node(doc, "p", "fm-git-empty", files.length ? "No conflicted files match this filter." : "No unresolved conflicts."));
  return result;
}

/** Choose a compact domain view, retaining every original field in expandable details. */
export function gitPresentation(data, toolName) {
  if (!isObject(data)) return { toolName, summary: data, records: null };
  const shape = Object.hasOwn(data, "head_before") ? "mutation"
    : data.commit && data.diff ? "git_show"
    : Array.isArray(data.entries) ? "git_status"
    : Array.isArray(data.commits) ? "git_log"
    : typeof data.patch === "string" && Array.isArray(data.files) ? "git_diff"
    : Array.isArray(data.lines) ? "git_blame"
    : Array.isArray(data.remotes) ? "git_remotes"
    : Array.isArray(data.refs) ? "refs"
    : Array.isArray(data.files) ? "git_conflicts"
    : Array.isArray(data.stashes) ? "git_stash" : "other";
  const inferredName = { mutation: "Git operation", refs: "Git refs", other: "Git result" }[shape] ?? shape;
  const total = data.stashes?.length ?? data.entries?.length ?? data.commits?.length ?? data.refs?.length ?? data.remotes?.length ?? data.lines?.length ?? data.files?.length ?? data.changes?.length;
  return {
    toolName: toolName === "git_status" && shape !== "git_status" ? inferredName : toolName,
    records: null,
    filterPlaceholder: "Filter paths, messages, authors or IDs",
    count: total === undefined ? "Captured Git result" : `${total} ${total === 1 ? "item" : "items"} · captured Git result`,
    render: (doc, query, width, tools = {}) => {
      const result = node(doc, "div", "fm-git");
      if (shape === "git_status") result.append(statusView(doc, data, query, tools));
      else if (shape === "git_log") result.append(historyView(doc, data, query, tools));
      else if (shape === "git_diff") result.append(diffView(doc, data, query, tools));
      else if (shape === "git_show") {
        if (!query || matches(data.commit, query) || matches(data.diff, query)) {
          result.append(commitCard(doc, data.commit, tools));
          result.append(diffView(doc, data.diff, matches(data.commit, query) ? "" : query, tools));
        } else result.append(node(doc, "p", "fm-git-empty", "No commit details match this filter."));
      } else if (shape === "refs") result.append(refsView(doc, data, query, tools));
      else if (shape === "git_remotes") result.append(remotesView(doc, data, query));
      else if (shape === "git_blame") result.append(blameView(doc, data, query, tools));
      else if (shape === "git_conflicts") result.append(conflictsView(doc, data, query, tools));
      else if (shape === "mutation") result.append(mutationView(doc, data, query, tools));
      else if (shape === "git_stash") result.append(stashesView(doc, data.stashes, query, tools));
      else result.append(detailsFields(doc, "Git result", data, tools));
      result.append(detailsFields(doc, "Result details · all original fields", data, tools));
      return result;
    },
  };
}

/** Keep literal source values readable inside expandable original Fields details. */
export function renderGitValue(value, key, record, { doc }) {
  if (typeof value !== "string") return null;
  if (["error", "synchronization_error"].includes(key)) return node(doc, "span", "fm-value fm-error", value);
  if (!["patch", "text", "message"].includes(key)) return null;
  const result = node(doc, "div", "fm-source");
  result.append(node(doc, "pre", "fm-value", value));
  return result;
}
