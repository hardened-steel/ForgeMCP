import {
  App,
  PostMessageTransport,
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
} from "@modelcontextprotocol/ext-apps";

import "./process-overview.css";

const app = new App({ name: "ForgeMCP process overview", version: "0.2.0" });

const setText = (id, value) => {
  document.getElementById(id).textContent = String(value);
};

const renderProcess = (process) => {
  const article = document.createElement("article");
  article.className = "process";

  const heading = document.createElement("div");
  heading.className = "process-heading";

  const command = document.createElement("strong");
  command.textContent = [process.executable, ...(process.arguments ?? [])].join(" ");

  const state = document.createElement("span");
  state.className = `state state-${process.status}`;
  state.textContent = process.status.replaceAll("_", " ");

  heading.append(command, state);

  const details = document.createElement("dl");
  const rows = [
    ["Process", process.process_id],
    ["PID", process.pid ?? "—"],
    ["Return code", process.return_code ?? "—"],
    ["Encoding", process.encoding],
  ];
  for (const [label, value] of rows) {
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = String(value);
    details.append(term, description);
  }

  article.append(heading, details);
  return article;
};

const render = (result) => {
  const data = result?.structuredContent;
  if (!data) return;

  setText("running-count", data.running);
  setText("completed-count", data.completed);
  const container = document.getElementById("processes");
  container.replaceChildren(...(data.processes ?? []).map(renderProcess));
  setText(
    "status",
    data.processes?.length ? `${data.processes.length} processes shown.` : "No processes recorded yet.",
  );
};

const applyHostContext = (context) => {
  if (!context) return;
  if (context.theme) applyDocumentTheme(context.theme);
  if (context.styles?.variables) applyHostStyleVariables(context.styles.variables);
  if (context.styles?.css?.fonts) applyHostFonts(context.styles.css.fonts);
  if (context.safeAreaInsets) {
    const { top, right, bottom, left } = context.safeAreaInsets;
    document.body.style.padding = `${top}px ${right}px ${bottom}px ${left}px`;
  }
};

app.addEventListener("toolinput", () => setText("status", "Reading process state…"));
app.addEventListener("toolresult", render);
app.addEventListener("hostcontextchanged", applyHostContext);
app.onteardown = async () => ({});

await app.connect(new PostMessageTransport());
applyHostContext(app.getHostContext());
