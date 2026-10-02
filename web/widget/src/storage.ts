/** Persistence for the visitor id and conversation, with an in-memory fallback. */

export interface KeyValue {
  get(key: string): string | null;
  set(key: string, value: string): void;
}

export class MemoryStore implements KeyValue {
  private values = new Map<string, string>();
  get(key: string): string | null {
    return this.values.get(key) ?? null;
  }
  set(key: string, value: string): void {
    this.values.set(key, value);
  }
}

/** localStorage when it works (it throws in some privacy modes and sandboxes), else memory. */
export function createStore(storage?: () => Storage | null): KeyValue {
  try {
    const local = storage ? storage() : window.localStorage;
    if (!local) return new MemoryStore();
    const probe = "__bap_probe__";
    local.setItem(probe, "1");
    local.removeItem(probe);
    return {
      get: (key) => {
        try {
          return local.getItem(key);
        } catch {
          return null;
        }
      },
      set: (key, value) => {
        try {
          local.setItem(key, value);
        } catch {
          // quota exceeded or storage revoked: keep working without persistence
        }
      },
    };
  } catch {
    return new MemoryStore();
  }
}

export interface StoredMessage {
  role: "user" | "assistant";
  text: string;
  clientId?: string; // customer messages: the client_message_id sent with it
  at: number; // epoch milliseconds
  citations?: { title: string; detail: string; snippet: string }[];
}

export interface Session {
  visitorId: string;
  conversationId: string | null;
  messages: StoredMessage[];
}

const MAX_MESSAGES = 50;

export function newVisitorId(): string {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi?.randomUUID) return cryptoApi.randomUUID();
  return `v-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

export class SessionStore {
  private readonly kv: KeyValue;
  private readonly key: string;

  constructor(kv: KeyValue, namespace: string) {
    this.kv = kv;
    this.key = `bap-widget:${namespace}`;
  }

  load(): Session {
    try {
      const raw = this.kv.get(this.key);
      if (raw) {
        const parsed = JSON.parse(raw) as Partial<Session>;
        if (typeof parsed.visitorId === "string" && parsed.visitorId) {
          return {
            visitorId: parsed.visitorId,
            conversationId: typeof parsed.conversationId === "string" ? parsed.conversationId : null,
            messages: Array.isArray(parsed.messages) ? parsed.messages.slice(-MAX_MESSAGES) : [],
          };
        }
      }
    } catch {
      // corrupt entry: start a fresh session
    }
    const session = { visitorId: newVisitorId(), conversationId: null, messages: [] };
    this.save(session);
    return session;
  }

  save(session: Session): void {
    const trimmed = { ...session, messages: session.messages.slice(-MAX_MESSAGES) };
    try {
      this.kv.set(this.key, JSON.stringify(trimmed));
    } catch {
      // storage failed mid-session: the conversation still works for this page view
    }
  }
}
