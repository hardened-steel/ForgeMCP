/** Build typed admin arguments directly from the SDK's JSON Schema. */
let nextInputId = 0;
const omitted = Symbol("omitted");

/** Create text-only markup, including descriptions supplied by a server. */
function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}

/** Resolve local schema definitions without fetching any external resources. */
function resolveSchema(schema, root, seen = new Set()) {
  if (!schema || typeof schema !== "object") return {};
  if (schema.$ref?.startsWith("#/") && !seen.has(schema.$ref)) {
    const reference = schema.$ref;
    const target = reference.slice(2).split("/").reduce(
      (value, key) => value?.[key.replace(/~1/g, "/").replace(/~0/g, "~")], root,
    );
    return { ...resolveSchema(target, root, new Set([...seen, reference])), ...schema, $ref: undefined };
  }
  return schema;
}

/** Expand nullable types and ordinary unions into selectable input variants. */
function variants(schema, root) {
  const resolved = resolveSchema(schema, root);
  const choices = resolved.oneOf ?? resolved.anyOf;
  if (choices) return choices.flatMap((choice) => variants({ ...resolved, oneOf: undefined, anyOf: undefined, ...resolveSchema(choice, root) }, root));
  if (Array.isArray(resolved.type)) return resolved.type.map((type) => ({ ...resolved, type }));
  return [resolved];
}

/** Infer the common object/array shape when a schema omits its type keyword. */
function schemaType(schema) {
  return schema.type ?? (schema.properties || schema.additionalProperties ? "object" : schema.items ? "array" : "unknown");
}

/** Name a choice with its model title, literal value, or concrete input type. */
function variantName(schema) {
  if (schemaType(schema) === "null") return "null";
  if (Object.hasOwn(schema, "const")) return JSON.stringify(schema.const);
  if (schema.title) return schema.title.replace(/([a-z])([A-Z])/g, "$1 $2");
  const type = schemaType(schema);
  if (type === "object" && schema.properties) return Object.keys(schema.properties).join(" + ") || "Object";
  return type === "unknown" ? "JSON value" : type[0].toUpperCase() + type.slice(1);
}

/** Match supplied values to union variants without coercing their original types. */
function matches(value, schema, root) {
  const resolved = resolveSchema(schema, root);
  if (resolved.oneOf || resolved.anyOf || Array.isArray(resolved.type)) return variants(resolved, root).some((choice) => matches(value, choice, root));
  if (Object.hasOwn(resolved, "const")) return value === resolved.const;
  if (resolved.enum && !resolved.enum.some((choice) => choice === value)) return false;
  const type = schemaType(resolved);
  if (type === "null") return value === null;
  if (type === "array") return Array.isArray(value);
  if (type === "integer") return Number.isInteger(value);
  if (type === "number") return typeof value === "number" && Number.isFinite(value);
  if (type === "object") {
    return value !== null && typeof value === "object" && !Array.isArray(value)
      && (resolved.required ?? []).every((key) => Object.hasOwn(value, key))
      && Object.entries(value).every(([key, item]) => !resolved.properties?.[key] || matches(item, resolved.properties[key], root));
  }
  return type === "unknown" || typeof value === type;
}

/** Make a short readable default label while keeping full values in the schema view. */
function defaultLabel(schema) {
  if (!Object.hasOwn(schema, "default")) return "Not set";
  const value = JSON.stringify(schema.default);
  return `Default: ${value.length > 36 ? `${value.slice(0, 33)}…` : value}`;
}

/** Explain a field error and move focus to the input that can correct it. */
function invalid(input, path, message) {
  input?.setAttribute("aria-invalid", "true");
  input?.focus();
  throw new Error(`${path}: ${message}`);
}

/** Create a schema form; reading validates values while omitted fields keep server defaults. */
export function createSchemaForm(schema, initialValue = {}, { onChange = () => {} } = {}) {
  const element = node("div", "admin-schema-form");
  const root = schema ?? { type: "object", properties: {} };
  let editor;

  /** Clear stale input errors and notify the command composer without submitting it. */
  function changed(event) {
    event?.target?.removeAttribute("aria-invalid");
    onChange();
  }

  /** Render one named argument, including its optional/nullable/union state. */
  function field(definition, value, required, name, path, depth) {
    const resolved = resolveSchema(definition, root);
    if (required && value === undefined && Object.hasOwn(resolved, "default")) value = resolved.default;
    const choices = variants(resolved, root);
    let matched = choices.findIndex((choice) => matches(value, choice, root));
    if (value !== undefined && matched === -1) {
      matched = choices.length;
      choices.push({ ...resolved, title: "Custom JSON", "x-admin-json": true });
    }
    const wrapper = node("div", "admin-form-field");
    const header = node("div", "admin-form-field-header");
    const id = `admin-argument-${++nextInputId}`;
    const label = node("label", "admin-form-label", name);
    label.id = `${id}-label`;
    label.htmlFor = id;
    header.append(label);
    if (required) header.append(node("span", "admin-form-required", "Required"));
    const content = node("div", "admin-form-content");
    const help = node("p", "admin-form-help", resolved.description ?? "");
    help.id = `${id}-help`;
    help.hidden = !resolved.description;
    let selected = !required && value === undefined ? -1 : Math.max(0, matched);
    let input;
    const saved = new Map();
    let mode;
    if (!required || choices.length > 1) {
      mode = node("select", "admin-form-mode");
      mode.id = `${id}-mode`;
      mode.setAttribute("aria-label", `${path}: value mode`);
      if (!required) {
        const option = node("option", "", defaultLabel(resolved));
        option.value = "-1";
        mode.append(option);
      }
      const names = choices.map(variantName);
      choices.forEach((choice, index) => {
        const name = names.filter((item) => item === names[index]).length > 1
          ? `${names[index]} (${schemaType(choice) === "object" ? Object.keys(choice.properties ?? {}).join(" + ") || "object" : schemaType(choice)})`
          : names[index];
        const option = node("option", "", choices.length === 1 ? "Set value" : name);
        option.value = String(index);
        mode.append(option);
      });
      mode.value = String(selected);
      mode.addEventListener("change", () => {
        if (input) saved.set(selected, input.read(false));
        selected = Number(mode.value);
        render(saved.get(selected));
        changed();
      });
      header.append(mode);
    } else header.append(node("span", "admin-form-type", variantName(choices[0])));
    wrapper.append(header, help, content);
    wrapper.setAttribute("role", "group");
    wrapper.setAttribute("aria-labelledby", label.id);

    /** Replace only a changed union branch, preserving neighboring form values and focus. */
    function render(supplied) {
      content.replaceChildren();
      wrapper.dataset.omitted = String(selected === -1);
      input = null;
      if (selected === -1) {
        label.htmlFor = mode?.id ?? "";
        return;
      }
      const choice = choices[selected];
      const initial = supplied === undefined && Object.hasOwn(resolved, "default") && matches(resolved.default, choice, root) ? resolved.default : supplied;
      input = valueEditor(choice, initial, path, depth, id, help.id);
      content.append(input.element);
      if (content.querySelector(`#${id}`)) label.htmlFor = id;
      else if (mode) label.htmlFor = mode.id;
      else label.removeAttribute("for");
    }
    render(value);
    return {
      element: wrapper,
      read(validate = true) { return selected === -1 ? omitted : input.read(validate); },
    };
  }

  /** Build a concrete branch using native text, numeric, select, list, and object controls. */
  function valueEditor(definition, value, path, depth, id, descriptionId) {
    const type = schemaType(definition);
    if (definition["x-admin-json"]) return jsonEditor(definition, value, path, id, descriptionId);
    if (type === "null") return { element: node("span", "admin-muted", "null"), read: () => null };
    if (Object.hasOwn(definition, "const")) {
      return { element: node("span", "admin-form-type", JSON.stringify(definition.const)), read: () => definition.const };
    }
    if (depth > 12 || type === "unknown" || definition.$ref || definition.allOf) return jsonEditor(definition, value, path, id, descriptionId);
    if (type === "object") return objectEditor(definition, value, path, depth);
    if (type === "array") return arrayEditor(definition, value, path, depth);
    if (definition.enum) {
      const select = node("select");
      select.id = id;
      select.setAttribute("aria-describedby", descriptionId);
      const empty = node("option", "", "Choose a value…");
      empty.value = "";
      select.append(empty);
      definition.enum.forEach((choice, index) => {
        const option = node("option", "", typeof choice === "string" ? choice : JSON.stringify(choice));
        option.value = String(index);
        select.append(option);
      });
      const index = definition.enum.findIndex((choice) => choice === value);
      select.value = index === -1 ? "" : String(index);
      select.addEventListener("change", changed);
      return {
        element: select,
        read(validate = true) {
          if (select.value === "") {
            if (validate) invalid(select, path, "choose a value.");
            return undefined;
          }
          return definition.enum[Number(select.value)];
        },
      };
    }
    if (type === "boolean") {
      const checkbox = node("input");
      checkbox.type = "checkbox";
      checkbox.id = id;
      checkbox.checked = value === true;
      checkbox.setAttribute("aria-describedby", descriptionId);
      checkbox.addEventListener("change", changed);
      const label = node("label", "admin-form-checkbox");
      label.append(checkbox, document.createTextNode("Enabled"));
      return { element: label, read: () => checkbox.checked };
    }
    const multiline = type === "string" && (/text|content|message|patch|script/i.test(path.split(".").at(-1)) || String(value ?? "").includes("\n"));
    const input = node(multiline ? "textarea" : "input");
    input.id = id;
    input.setAttribute("aria-describedby", descriptionId);
    input.spellcheck = false;
    if (multiline) input.rows = 4;
    else input.type = type === "integer" || type === "number" ? "number" : "text";
    if (input.type === "number") {
      input.step = type === "integer" ? "1" : "any";
      if (definition.minimum !== undefined) input.min = String(definition.minimum);
      if (definition.maximum !== undefined) input.max = String(definition.maximum);
    }
    input.value = value === undefined ? "" : String(value);
    if (definition.examples?.[0] !== undefined) input.placeholder = String(definition.examples[0]);
    input.addEventListener("input", changed);
    return {
      element: input,
      read(validate = true) {
        if (type === "integer" || type === "number") {
          const result = input.value === "" ? undefined : Number(input.value);
          if (!validate) return result;
          if (!Number.isFinite(result)) invalid(input, path, "enter a number.");
          if (type === "integer" && !Number.isInteger(result)) invalid(input, path, "enter a whole number.");
          if (definition.minimum !== undefined && result < definition.minimum) invalid(input, path, `minimum is ${definition.minimum}.`);
          if (definition.maximum !== undefined && result > definition.maximum) invalid(input, path, `maximum is ${definition.maximum}.`);
          if (definition.exclusiveMinimum !== undefined && result <= definition.exclusiveMinimum) invalid(input, path, `must be greater than ${definition.exclusiveMinimum}.`);
          if (definition.exclusiveMaximum !== undefined && result >= definition.exclusiveMaximum) invalid(input, path, `must be less than ${definition.exclusiveMaximum}.`);
          return result;
        }
        if (validate && definition.minLength !== undefined && input.value.length < definition.minLength) invalid(input, path, `enter at least ${definition.minLength} characters.`);
        if (validate && definition.maxLength !== undefined && input.value.length > definition.maxLength) invalid(input, path, `use at most ${definition.maxLength} characters.`);
        return input.value;
      },
    };
  }

  /** Render object members and preserve additional supplied keys for advanced inputs. */
  function objectEditor(definition, value, path, depth) {
    const container = node("div", "admin-form-object");
    const supplied = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    const fields = Object.entries(definition.properties ?? {}).map(([name, child]) => {
      const editor = field(child, Object.hasOwn(supplied, name) ? supplied[name] : undefined, (definition.required ?? []).includes(name), name, path ? `${path}.${name}` : name, depth + 1);
      container.append(editor.element);
      return [name, editor];
    });
    const extra = Object.fromEntries(Object.entries(supplied).filter(([name]) => !Object.hasOwn(definition.properties ?? {}, name)));
    let extraEditor;
    if (definition.additionalProperties !== false && (definition.additionalProperties || Object.keys(extra).length)) {
      const title = node("label", "admin-form-label", "Additional properties (JSON object)");
      const id = `admin-argument-${++nextInputId}`;
      title.htmlFor = id;
      extraEditor = jsonEditor({ type: "object" }, extra, path || "Arguments", id);
      container.append(title, extraEditor.element);
    }
    if (!fields.length && !extraEditor) container.append(node("p", "admin-form-empty", "No parameters. This tool is ready to run."));
    return {
      element: container,
      read(validate = true) {
        if (validate && definition.additionalProperties === false && Object.keys(extra).length) throw new Error(`${path || "Arguments"}: unknown fields ${Object.keys(extra).join(", ")}; use JSON mode to correct them.`);
        const suppliedExtra = extraEditor ? extraEditor.read(validate) : extra;
        const result = suppliedExtra && typeof suppliedExtra === "object" && !Array.isArray(suppliedExtra)
          ? { ...suppliedExtra } : { ...extra };
        for (const [name, editor] of fields) {
          const item = editor.read(validate);
          if (item !== omitted) Object.defineProperty(result, name, { value: item, enumerable: true, configurable: true, writable: true });
        }
        return result;
      },
    };
  }

  /** Let operators add or remove typed array items without writing JSON delimiters. */
  function arrayEditor(definition, value, path, depth) {
    const container = node("div", "admin-form-array");
    const items = node("div", "admin-form-array-items");
    const empty = node("p", "admin-form-empty", "Empty list");
    const editors = [];
    const add = node("button", "admin-form-add", "+ Add item");
    add.type = "button";

    /** Update item numbers and list bounds after a local edit. */
    function update() {
      empty.hidden = editors.length > 0;
      add.disabled = definition.maxItems !== undefined && editors.length >= definition.maxItems;
      editors.forEach((item, index) => {
        item.element.querySelector(".admin-form-label").textContent = `Item ${index + 1}`;
        item.remove.setAttribute("aria-label", `Remove ${path} item ${index + 1}`);
      });
    }

    /** Append an independent list editor and expose a labeled removal action. */
    function append(supplied) {
      const wrapper = node("div", "admin-form-array-item");
      const editor = field(definition.items ?? {}, supplied, true, `Item ${editors.length + 1}`, `${path}[]`, depth + 1);
      const remove = node("button", "admin-form-remove", "Remove");
      remove.type = "button";
      const entry = { ...editor, element: wrapper, remove };
      remove.addEventListener("click", () => {
        const index = editors.indexOf(entry);
        editors.splice(index, 1);
        wrapper.remove();
        update();
        changed();
      });
      wrapper.append(editor.element, remove);
      editors.push(entry);
      items.append(wrapper);
      update();
    }
    for (const supplied of Array.isArray(value) ? value : []) append(supplied);
    add.addEventListener("click", () => { append(undefined); changed(); });
    container.append(items, empty, add);
    update();
    return {
      element: container,
      read(validate = true) {
        if (validate && definition.minItems !== undefined && editors.length < definition.minItems) invalid(add, path, `add at least ${definition.minItems} items.`);
        if (validate && definition.maxItems !== undefined && editors.length > definition.maxItems) invalid(add, path, `use at most ${definition.maxItems} items.`);
        return editors.map((editor) => editor.read(validate));
      },
    };
  }

  /** Retain an explicit JSON escape hatch for schemas that have no concrete native editor. */
  function jsonEditor(definition, value, path, id, descriptionId) {
    const input = node("textarea", "admin-form-json");
    input.id = id;
    input.rows = 4;
    input.spellcheck = false;
    if (descriptionId) input.setAttribute("aria-describedby", descriptionId);
    input.value = value === undefined ? "" : JSON.stringify(value, null, 2);
    input.placeholder = schemaType(definition) === "object" ? "{}" : "JSON value";
    input.addEventListener("input", changed);
    return {
      element: input,
      read(validate = true) {
        let result;
        try { result = JSON.parse(input.value); }
        catch {
          if (validate) invalid(input, path, "enter valid JSON.");
          return undefined;
        }
        if (validate && !matches(result, definition, root)) invalid(input, path, `expected ${schemaType(definition)}.`);
        return result;
      },
    };
  }

  /** Replace the displayed arguments when changing tools or returning from JSON mode. */
  function setValue(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Tool arguments must be a JSON object.");
    editor = objectEditor(resolveSchema(root, root), value, "", 0);
    element.replaceChildren(editor.element);
  }

  setValue(initialValue);
  return {
    element,
    getValue(validate = true) {
      if (validate) element.querySelectorAll("[aria-invalid]").forEach((input) => input.removeAttribute("aria-invalid"));
      return editor.read(validate);
    },
    setValue,
  };
}
