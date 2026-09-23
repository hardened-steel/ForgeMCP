import { connectWidget } from "./shared/app.js";
import { timestamp } from "./shared/presentation.js";
import "./process.css";

function describe(data) {
  let showAll = false;
  const presentation = {
    toolName: "processes_overview",
    summary: { running: data.running, completed: data.completed },
    records: data.processes,
    count: `${data.processes.length} processes`,
    render(doc, query) {
      const container = doc.createElement("div");
      const summary = doc.createElement("p");
      summary.className = "fm-process-summary";
      summary.textContent = `${data.running} running · ${data.completed} completed`;
      container.append(summary);
      const matches = data.processes.filter((item) => JSON.stringify(item).toLocaleLowerCase().includes(query));
      const visible = showAll ? matches : matches.slice(0, 10);
      for (const item of visible) {
        const row = doc.createElement("details");
        row.className = "fm-process-row";
        const heading = doc.createElement("summary");
        const outcome = doc.createElement("span");
        outcome.className = item.state === "running" ? "fm-active" : item.return_code === 0 ? "fm-success" : "fm-error";
        outcome.textContent = item.outcome;
        heading.append(`#${item.process_id}  ${item.executable}  ·  `, outcome);
        row.append(heading);
        const details = doc.createElement("dl");
        for (const [key, value] of [
          ["PID", item.pid], ["Arguments", item.arguments.join(" ") || "None"],
          ["Working directory", item.cwd], ["Started", timestamp(item.started)?.readable ?? item.started],
          ["Work time", `${item.work_time.toFixed(2)} s`], ["Encoding", item.encoding],
        ]) {
          const term = doc.createElement("dt");
          term.textContent = key;
          const description = doc.createElement("dd");
          description.textContent = String(value);
          details.append(term, description);
        }
        row.append(details);
        container.append(row);
      }
      if (!visible.length) {
        const empty = doc.createElement("p");
        empty.className = "fm-empty";
        empty.textContent = "No matching processes.";
        container.append(empty);
      }
      if (matches.length > 10) {
        const expand = doc.createElement("button");
        expand.type = "button";
        expand.textContent = showAll ? "Show first 10" : `Show all ${matches.length} processes`;
        expand.addEventListener("click", () => {
          showAll = !showAll;
          container.replaceWith(presentation.render(doc, query));
        });
        container.append(expand);
      }
      presentation.count = `${visible.length} / ${matches.length} shown`;
      return container;
    },
  };
  return presentation;
}

await connectWidget({ toolName: "processes_overview", describe });
