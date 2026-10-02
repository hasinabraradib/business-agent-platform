import "./setup.ts";
import { ApiClient } from "../src/api.ts";
import { type KeyValue, MemoryStore, SessionStore } from "../src/storage.ts";
import { Widget } from "../src/widget.ts";

export const encoder = new TextEncoder();

export function sse(events: [string, unknown][]): string {
  return events.map(([name, data]) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`).join("");
}

/** A body that delivers the given text in fixed-size byte chunks (splitting events and
 * multi-byte characters at arbitrary points). */
export function byteStream(text: string, chunkSize = 7): ReadableStream<Uint8Array> {
  const bytes = encoder.encode(text);
  let offset = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (offset >= bytes.length) return controller.close();
      controller.enqueue(bytes.slice(offset, offset + chunkSize));
      offset += chunkSize;
    },
  });
}

export interface FakeResponse {
  status?: number;
  sse?: string;
  json?: unknown;
  headers?: Record<string, string>;
  reject?: boolean;
  chunkSize?: number;
}

export interface Call {
  url: string;
  init: RequestInit | undefined;
  body: Record<string, unknown> | null;
}

/** fetch() stand-in: answers /v1/widget/config and queued /v1/chat responses, records calls. */
export function fakeFetch(config: Record<string, unknown> | FakeResponse, chat: FakeResponse[]) {
  const calls: Call[] = [];
  const fetchFn = async (url: string, init?: RequestInit): Promise<Response> => {
    calls.push({ url, init, body: init?.body ? JSON.parse(String(init.body)) : null });
    let spec: FakeResponse;
    if (url.endsWith("/v1/widget/config")) {
      spec = "status" in config ? (config as FakeResponse) : { json: config };
    }
    else {
      const next = chat.shift();
      if (!next) throw new Error("no fake chat response queued");
      spec = next;
    }
    if (spec.reject) throw new TypeError("Failed to fetch");
    const status = spec.status ?? 200;
    const headers = new Headers(spec.headers ?? {});
    return {
      ok: status >= 200 && status < 300,
      status,
      headers,
      body: spec.sse !== undefined ? byteStream(spec.sse, spec.chunkSize) : null,
      json: async () => spec.json,
    } as unknown as Response;
  };
  return { fetchFn, calls };
}

export const CONFIG = {
  assistant_name: "Nodi",
  business_name: "Nodi Kitchen",
  greeting: "Assalamu alaikum! Ask me anything.",
  accent_color: "#C5EE4F",
  suggested_questions: ["What's on the menu?", "শুক্রবার কখন খোলেন?"],
};

export function mountWidget(
  chat: FakeResponse[],
  options: { config?: Record<string, unknown> | FakeResponse; kv?: KeyValue } = {},
) {
  document.body.replaceChildren();
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = host.attachShadow({ mode: "open" });
  const fake = fakeFetch(options.config ?? CONFIG, chat);
  const kv = options.kv ?? new MemoryStore();
  const widget = new Widget({
    root,
    host,
    api: new ApiClient("https://api.example/", "bap_widget_testkey123456", fake.fetchFn),
    sessions: new SessionStore(kv, "testkey123456"),
    now: () => Date.UTC(2026, 9, 3, 9, 30),
  });
  return { widget, root, host, kv, calls: fake.calls, chat };
}

export function bubbles(root: ShadowRoot, role: string): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>(`.msg.${role} .bubble`)];
}

export const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
