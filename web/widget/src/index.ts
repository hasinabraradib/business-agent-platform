/**
 * Embed: <script src="https://api.example/widget.js" data-key="bap_widget_..." async></script>
 * Optional data-api sets the API base URL (default: the script's own origin).
 */
import { ApiClient } from "./api.ts";
import { createStore, SessionStore } from "./storage.ts";
import { Widget } from "./widget.ts";

declare global {
  interface Window {
    __bapWidget?: Widget;
  }
}

function findScript(): HTMLScriptElement | null {
  const current = document.currentScript;
  if (current instanceof HTMLScriptElement && current.dataset.key) return current;
  return document.querySelector<HTMLScriptElement>("script[data-key][src*='widget']");
}

export function mount(key: string, api: string): Widget {
  const host = document.createElement("div");
  host.id = "bap-chat-widget";
  document.body.appendChild(host);
  const root = host.attachShadow({ mode: "open" });
  const namespace = key.slice(-12); // per-widget storage; the key itself is public anyway
  return new Widget({
    root,
    host,
    api: new ApiClient(api, key),
    sessions: new SessionStore(createStore(), namespace),
  });
}

(() => {
  if (window.__bapWidget) return; // embedded twice
  const script = findScript();
  const key = script?.dataset.key;
  if (!script || !key) {
    console.warn("Chat widget: missing data-key on the <script> tag");
    return;
  }
  const api = script.dataset.api || new URL(script.src, window.location.href).origin;
  const start = () => {
    window.__bapWidget = mount(key, api);
  };
  if (document.body) start();
  else document.addEventListener("DOMContentLoaded", start, { once: true });
})();
