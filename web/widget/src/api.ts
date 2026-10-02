import { readEvents } from "./sse.ts";

export interface WidgetConfig {
  offline?: boolean; // the API runs an offline demo model
  assistant_name: string;
  business_name: string;
  greeting: string;
  accent_color: string;
  suggested_questions: string[];
}

export interface Citation {
  marker: number;
  chunk_id: string;
  document_title: string;
  metadata: Record<string, unknown>;
  snippet: string;
}

export interface ChatHandlers {
  onToken(text: string): void;
  onCitations(citations: Citation[]): void;
  onDone(done: { message_id: string; conversation_id: string; outcome: string }): void;
  onError(error: { message: string; conversation_id?: string }): void;
}

export interface ChatRequest {
  visitor_id: string;
  message: string;
  conversation_id?: string;
  client_message_id?: string; // the same id on a retry, so the message is stored once
}

export class ApiError extends Error {
  readonly status: number;
  readonly retryAfter: number | null;

  constructor(status: number, message: string, retryAfter: number | null = null) {
    super(message);
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

type Fetch = (input: string, init?: RequestInit) => Promise<Response>;

export class ApiClient {
  private readonly base: string;
  private readonly key: string;
  private readonly fetchFn: Fetch;

  constructor(base: string, key: string, fetchFn?: Fetch) {
    this.base = base.replace(/\/+$/, "");
    this.key = key;
    this.fetchFn = fetchFn ?? ((input, init) => fetch(input, init));
  }

  private headers(extra: Record<string, string> = {}): Record<string, string> {
    return { Authorization: `Bearer ${this.key}`, ...extra };
  }

  private async fail(response: Response): Promise<never> {
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // not JSON: keep the generic message
    }
    const retry = Number(response.headers.get("retry-after"));
    throw new ApiError(response.status, detail, Number.isFinite(retry) && retry > 0 ? retry : null);
  }

  async config(): Promise<WidgetConfig> {
    const response = await this.fetchFn(`${this.base}/v1/widget/config`, {
      headers: this.headers(),
      credentials: "omit",
    });
    if (!response.ok) return this.fail(response);
    return (await response.json()) as WidgetConfig;
  }

  /** Send a message and dispatch the streamed reply. Resolves after done or error. */
  async chat(request: ChatRequest, handlers: ChatHandlers, signal?: AbortSignal): Promise<void> {
    const response = await this.fetchFn(`${this.base}/v1/chat`, {
      method: "POST",
      headers: this.headers({ "Content-Type": "application/json", Accept: "text/event-stream" }),
      body: JSON.stringify({ ...request, stream: true }),
      credentials: "omit",
      signal,
    });
    if (!response.ok) return this.fail(response);
    if (!response.body) throw new ApiError(0, "The reply could not be read");
    for await (const { event, data } of readEvents(response.body)) {
      const payload = JSON.parse(data);
      if (event === "token") handlers.onToken(String(payload.text ?? ""));
      else if (event === "citations") handlers.onCitations(payload.citations ?? []);
      else if (event === "done") return handlers.onDone(payload);
      else if (event === "error") return handlers.onError(payload);
    }
    throw new ApiError(0, "The connection closed before the reply finished");
  }
}
