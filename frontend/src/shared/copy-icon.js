/** Inline geometry avoids image requests and CSS-mask support in embedded hosts. */
export function createCopyIcon(document) {
  const element = (name, attributes) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    return node;
  };
  const svg = element("svg", {
    class: "fm-icon", viewBox: "0 0 24 24", width: "14", height: "14",
    fill: "none", stroke: "currentColor", "stroke-width": "1.6",
    "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": "true", focusable: "false",
  });
  svg.append(
    element("rect", { x: "8", y: "8", width: "13", height: "13", rx: "2" }),
    element("path", { d: "M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3" }),
  );
  return svg;
}
