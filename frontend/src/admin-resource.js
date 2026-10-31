import { UriTemplate } from "@modelcontextprotocol/sdk/shared/uriTemplate.js";

let nextResourceInput = 0;

/** Create text-only resource markup without interpreting server data as HTML. */
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

/** Describe template variable modifiers while leaving URI expansion to the SDK. */
function parameterOptions(template) {
  const options = new Map();
  for (const match of template.matchAll(/\{([^}]+)\}/g)) {
    const expression = match[1];
    const operator = /^[+#./?&]/.test(expression) ? expression[0] : "";
    for (const variable of expression.slice(operator.length).split(",")) {
      const name = variable.replace(/\*$/, "");
      const previous = options.get(name);
      options.set(name, {
        required: previous?.required || !["?", "&"].includes(operator),
        path: previous?.path || (operator === "/" && variable.endsWith("*")),
      });
    }
  }
  return options;
}

/** Read a static resource or compose a template with native server completions. */
export function createResourceView(resource, { readResource, complete }) {
  const view = node("section", "admin-resource-view");
  const form = node("form", "admin-resource-form");
  const parameters = node("div", "admin-resource-parameters");
  const preview = node("code", "admin-resource-preview");
  const result = node("div", "admin-resource-result");
  result.setAttribute("aria-live", "polite");
  const uri = resource.uriTemplate ?? resource.uri;
  let template;
  let options;
  try {
    if (typeof uri !== "string") throw new Error("This resource has no URI.");
    template = resource.uriTemplate ? new UriTemplate(uri) : null;
    options = parameterOptions(template ? uri : "");
  } catch (error) {
    view.append(node("p", "admin-error", `Resource template unavailable: ${error.message}`));
    return view;
  }
  const inputs = new Map();
  const pending = new Set();
  let revision = 0;
  let readRevision = 0;
  const submit = node("button", "admin-primary", "Read resource");
  submit.type = "submit";

  /** Keep completion context as literal strings, excluding omitted parameters. */
  function values() {
    return Object.fromEntries([...inputs].filter(([, input]) => input.value !== "").map(([name, input]) => [name, input.value]));
  }

  /** Encode full path segments correctly and omit empty optional query arguments. */
  function expandedUri() {
    if (!template) return uri;
    const supplied = values();
    for (const [name, option] of options) {
      if (option.path && supplied[name] !== undefined) supplied[name] = supplied[name].split("/");
    }
    return template.expand(supplied);
  }

  /** Invalidate prior suggestions when any contextual parameter changes. */
  function update() {
    revision++;
    for (const hide of pending) hide();
    try { preview.textContent = expandedUri(); }
    catch (error) { preview.textContent = `Cannot expand URI: ${error.message}`; }
  }

  /** Attach one keyboard-accessible server suggestion list to its parameter input. */
  function completionInput(name, input, wrapper, hint) {
    const list = node("div", "admin-completions");
    list.id = `${input.id}-completions`;
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", `${name} suggestions`);
    list.hidden = true;
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-controls", list.id);
    input.setAttribute("aria-expanded", "false");
    let timer;
    let query = 0;
    let choices = [];
    let selected = -1;

    /** Close stale suggestions without moving focus out of the parameter field. */
    function close() {
      clearTimeout(timer);
      query++;
      choices = [];
      selected = -1;
      list.hidden = true;
      list.replaceChildren();
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      hint.textContent = "Suggestions from the server appear as you type.";
    }
    pending.add(close);

    /** Commit the full suggested parameter and preserve ordinary editable input. */
    function choose(index) {
      if (choices[index] === undefined) return;
      input.value = choices[index];
      update();
      input.focus();
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    /** Mark the active option for keyboard and assistive technology users. */
    function highlight(index) {
      selected = index;
      [...list.children].forEach((option, current) => option.setAttribute("aria-selected", String(current === selected)));
      const active = list.children[selected];
      if (active) {
        input.setAttribute("aria-activedescendant", active.id);
        active.scrollIntoView({ block: "nearest" });
      } else input.removeAttribute("aria-activedescendant");
    }

    /** Ignore responses for an old prefix, context, detached view, or lost focus. */
    async function suggest() {
      const current = ++query;
      const contextRevision = revision;
      if (!view.isConnected || document.activeElement !== input) return;
      hint.textContent = "Loading suggestions…";
      try {
        const context = values();
        delete context[name];
        const response = await complete(
          { type: "ref/resource", uri },
          { name, value: input.value },
          context,
        );
        if (current !== query || contextRevision !== revision || !view.isConnected || document.activeElement !== input) return;
        const completion = response?.completion ?? response;
        choices = Array.isArray(completion?.values) ? completion.values.filter((value) => typeof value === "string") : [];
        choices = [...new Set(choices)];
        list.replaceChildren();
        selected = -1;
        for (const [index, value] of choices.entries()) {
          const option = node("button", "admin-completion-option", value);
          option.type = "button";
          option.id = `${list.id}-${index}`;
          option.tabIndex = -1;
          option.setAttribute("role", "option");
          option.setAttribute("aria-selected", "false");
          option.addEventListener("pointerdown", (event) => event.preventDefault());
          option.addEventListener("click", () => choose(index));
          list.append(option);
        }
        list.hidden = choices.length === 0;
        input.setAttribute("aria-expanded", String(choices.length > 0));
        const more = completion?.hasMore ?? completion?.has_more;
        hint.textContent = choices.length
          ? `${choices.length} suggestions${more ? "; type more to narrow the list" : ""}. Use ↑ ↓ and Enter to choose.`
          : "No suggestions for this value. You can enter it manually.";
      } catch (error) {
        if (current !== query || contextRevision !== revision || !view.isConnected) return;
        close();
        hint.textContent = `Suggestions unavailable: ${error.message}. You can enter a value manually.`;
      }
    }

    input.addEventListener("focus", () => { void suggest(); });
    input.addEventListener("input", () => {
      update();
      timer = setTimeout(() => { void suggest(); }, 220);
    });
    input.addEventListener("blur", close);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { event.preventDefault(); close(); return; }
      if (list.hidden || !choices.length) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const next = selected === -1 ? (event.key === "ArrowDown" ? 0 : choices.length - 1)
          : (selected + (event.key === "ArrowDown" ? 1 : -1) + choices.length) % choices.length;
        highlight(next);
      } else if (event.key === "Enter" && selected >= 0) {
        event.preventDefault();
        choose(selected);
      }
    });
    wrapper.append(list);
  }

  for (const name of new Set(template?.variableNames ?? [])) {
    const field = node("div", "admin-resource-field");
    const label = node("label", "admin-form-label", name);
    const wrapper = node("div", "admin-resource-input-wrap");
    const input = node("input", "admin-resource-input");
    input.id = `admin-resource-${++nextResourceInput}`;
    input.type = "text";
    input.spellcheck = false;
    input.autocomplete = "off";
    input.required = options.get(name)?.required ?? false;
    input.placeholder = options.get(name)?.path ? "project/path/to/file" : `Enter ${name}`;
    label.htmlFor = input.id;
    const hint = node("p", "admin-form-help admin-completion-hint", "Suggestions from the server appear as you type.");
    hint.id = `${input.id}-hint`;
    hint.setAttribute("role", "status");
    input.setAttribute("aria-describedby", hint.id);
    wrapper.append(input);
    inputs.set(name, input);
    if (complete) completionInput(name, input, wrapper, hint);
    else input.addEventListener("input", update);
    const header = node("div", "admin-form-field-header");
    header.append(label);
    if (input.required) header.append(node("span", "admin-form-required", "Required"));
    else header.append(node("span", "admin-form-type", "Optional"));
    field.append(header, wrapper, hint);
    parameters.append(field);
  }
  if (parameters.children.length) form.append(parameters);
  const previewLabel = node("div", "admin-resource-caption", "Resource URI");
  form.append(previewLabel, preview, node("div", "admin-resource-actions"));
  form.lastChild.append(submit);
  view.append(form, result);
  update();

  /** Display the resource returned for this exact URI, preserving its complete response. */
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submit.disabled || !form.reportValidity()) return;
    let selectedUri;
    try { selectedUri = expandedUri(); }
    catch (error) { result.replaceChildren(node("p", "admin-error", error.message)); return; }
    const current = ++readRevision;
    submit.disabled = true;
    submit.textContent = "Reading…";
    result.replaceChildren(node("p", "admin-muted", "Reading resource…"));
    try {
      const response = await readResource(selectedUri);
      if (current !== readRevision || !view.isConnected) return;
      result.replaceChildren(node("p", "admin-resource-caption", selectedUri));
      for (const content of response?.contents ?? []) {
        if (typeof content.text === "string") result.append(node("pre", "admin-pre", content.text));
        else if (typeof content.blob === "string") result.append(node("p", "admin-muted", `Binary resource (${content.mimeType ?? "application/octet-stream"}); base64 data is available in JSON.`));
      }
      const raw = node("details");
      raw.append(node("summary", "", "Complete resource JSON"), node("pre", "admin-pre", JSON.stringify(response, null, 2)));
      result.append(raw);
    } catch (error) {
      if (current === readRevision && view.isConnected) result.replaceChildren(node("p", "admin-error", error.message));
    } finally {
      submit.disabled = false;
      submit.textContent = "Read resource";
    }
  });
  return view;
}
