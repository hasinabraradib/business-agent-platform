/**
 * Safe rendering of model and server text. Nothing is ever parsed as HTML: text becomes text
 * nodes, citation markers like [1] become small superscripts, and only http(s) URLs become
 * links (javascript:, data:, vbscript: and everything else stay plain text).
 */

const URL_PATTERN = /\bhttps?:\/\/[^\s<>"'`]+/gi;
const TRAILING_PUNCTUATION = /[.,;:!?)\]}'"]+$/;
const MARKER = /\[(\d{1,2})\]/g;

export function safeHttpUrl(raw: string): URL | null {
  try {
    const url = new URL(raw);
    return url.protocol === "http:" || url.protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}

function appendWithMarkers(doc: Document, parent: Node, text: string, markers: boolean): void {
  if (!markers) {
    parent.appendChild(doc.createTextNode(text));
    return;
  }
  let last = 0;
  for (const match of text.matchAll(MARKER)) {
    const index = match.index ?? 0;
    if (index > last) parent.appendChild(doc.createTextNode(text.slice(last, index)));
    const sup = doc.createElement("sup");
    sup.className = "cite";
    sup.textContent = match[1];
    sup.setAttribute("aria-label", `source ${match[1]}`);
    parent.appendChild(sup);
    last = index + match[0].length;
  }
  if (last < text.length) parent.appendChild(doc.createTextNode(text.slice(last)));
}

export function renderText(doc: Document, text: string, options = { markers: true }): DocumentFragment {
  const fragment = doc.createDocumentFragment();
  let last = 0;
  for (const match of text.matchAll(URL_PATTERN)) {
    const index = match.index ?? 0;
    let raw = match[0];
    const trailing = TRAILING_PUNCTUATION.exec(raw)?.[0] ?? "";
    raw = raw.slice(0, raw.length - trailing.length);
    const url = safeHttpUrl(raw);
    if (!url) continue;
    appendWithMarkers(doc, fragment, text.slice(last, index), options.markers);
    const link = doc.createElement("a");
    link.href = url.href;
    link.textContent = raw;
    link.target = "_blank";
    link.rel = "noopener noreferrer nofollow ugc";
    fragment.appendChild(link);
    last = index + raw.length;
  }
  appendWithMarkers(doc, fragment, text.slice(last), options.markers);
  return fragment;
}
