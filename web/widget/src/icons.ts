/** Inline SVG icons built with createElementNS (fixed shapes, no markup parsing). */
const SVG_NS = "http://www.w3.org/2000/svg";

const PATHS = {
  chat: "M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.2 3.6c-.5.4-1.3.1-1.3-.6V16A2.5 2.5 0 0 1 4 13.5v-8Z",
  close: "M6 6l12 12M18 6 6 18",
  send: "M5 12h13M13 6l6 6-6 6",
  doc: "M7 3h7l4 4v14H7zM14 3v4h4M9.5 12h5M9.5 16h5",
} as const;

export function icon(doc: Document, name: keyof typeof PATHS): SVGSVGElement {
  const svg = doc.createElementNS(SVG_NS, "svg") as SVGSVGElement;
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", name === "chat" ? "currentColor" : "none");
  svg.setAttribute("stroke", name === "chat" ? "none" : "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const path = doc.createElementNS(SVG_NS, "path");
  path.setAttribute("d", PATHS[name]);
  svg.appendChild(path);
  return svg;
}
