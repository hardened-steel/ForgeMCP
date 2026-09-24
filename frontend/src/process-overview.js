import { connectWidget } from "./shared/app.js";
import { timestamp } from "./shared/presentation.js";
import { processStatus } from "./process-status.js";
import "./process.css";

function describe(data) {
  let showAll = false;
  let newestFirst = false;
  const presentation = {
    toolName: "processes_overview",
    summary: { running: data.running, completed: data.completed },
    records: data.processes,
    count: `${data.processes.length} processes`,
    render(doc, query) {
      const container = doc.createElement("div");
      const controls = doc.createElement("div");
      controls.className = "fm-process-controls";
      const summary = doc.createElement("p");
      summary.className = "fm-process-summary";
      summary.textContent = `${data.running} running · ${data.completed} completed`;
      const expand = doc.createElement("button");
      expand.type = "button";
      expand.className = "fm-process-action";
      expand.textContent = showAll ? "Collapse list" : "Expand list";
      expand.setAttribute("aria-expanded", String(showAll));
      expand.addEventListener("click", () => {
        showAll = !showAll;
        container.replaceWith(presentation.render(doc, query));
      });
      const order = doc.createElement("button");
      order.type = "button";
      order.className = "fm-process-action";
      order.textContent = newestFirst ? "Oldest first" : "Newest first";
      order.setAttribute("aria-pressed", String(newestFirst));
      order.addEventListener("click", () => {
        newestFirst = !newestFirst;
        container.replaceWith(presentation.render(doc, query));
      });
      controls.append(summary, expand, order);
      container.append(controls);
      const matches = data.processes.filter((item) => JSON.stringify(item).toLocaleLowerCase().includes(query));
      const ordered = newestFirst ? [...matches].reverse() : matches;
      const visible = showAll ? ordered : ordered.slice(0, 10);
      for (const item of visible) {
        const command = item.summary;
        const status = item.status;
        const row = doc.createElement("details");
        row.className = "fm-process-row";
        row.open = showAll;
        const heading = doc.createElement("summary");
        const outcome = doc.createElement("span");
        const display = processStatus(status.current_status);
        outcome.className = display.tone;
        outcome.textContent = display.label;
        heading.append(`#${command.process_id}  ${command.executable}  ·  `, outcome);
        row.append(heading);
        const details = doc.createElement("dl");
        for (const [key, value] of [
          ["PID", status.pid], ["Arguments", command.arguments.join(" ") || "None"],
          ["Working directory", command.cwd], ["Started", timestamp(status.started)?.readable ?? status.started],
          ["Work time", `${status.work_time.toFixed(2)} s`], ["Encoding", command.encoding],
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
      presentation.count = `${matches.length} matching processes`;
      return container;
    },
  };
  return presentation;
}

await connectWidget({ toolName: "processes_overview", describe });
