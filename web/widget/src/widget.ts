import { ApiClient, ApiError, type Citation, type WidgetConfig } from "./api.ts";
import { readableTextOn, safeAccent } from "./color.ts";
import { icon } from "./icons.ts";
import { renderText } from "./render.ts";
import { newVisitorId, type Session, SessionStore, type StoredMessage } from "./storage.ts";
import { widgetCss } from "./styles.ts";
import { tokens } from "./tokens.ts";

export interface WidgetOptions {
  root: ShadowRoot;
  host: HTMLElement;
  api: ApiClient;
  sessions: SessionStore;
  now?: () => number;
}

const DEFAULT_CONFIG: WidgetConfig = {
  assistant_name: "Assistant",
  business_name: "",
  greeting: "Hi! How can I help you today?",
  accent_color: tokens.color.accent,
  suggested_questions: [],
};

type SourceChip = NonNullable<StoredMessage["citations"]>[number];

function sourceChip(citation: Citation): SourceChip {
  const meta = citation.metadata ?? {};
  let detail = "";
  if (typeof meta.section === "string") detail = meta.section.split(" > ").pop() ?? "";
  else if (meta.row !== undefined) detail = `row ${meta.row}`;
  else if (meta.page !== undefined) detail = `page ${meta.page}`;
  return { title: citation.document_title, detail, snippet: citation.snippet ?? "" };
}

export class Widget {
  private readonly doc: Document;
  private readonly root: ShadowRoot;
  private readonly host: HTMLElement;
  private readonly api: ApiClient;
  private readonly sessions: SessionStore;
  private readonly now: () => number;
  private readonly session: Session;
  private config: WidgetConfig = DEFAULT_CONFIG;
  private configLoaded = false;
  private busy = false;
  private lastQuestion: { text: string; clientId: string } | null = null;

  readonly launcher: HTMLButtonElement;
  readonly panel: HTMLElement;
  readonly list: HTMLElement;
  readonly input: HTMLTextAreaElement;
  readonly sendButton: HTMLButtonElement;
  readonly closeButton: HTMLButtonElement;
  readonly live: HTMLElement;
  private readonly nameEl: HTMLElement;
  private readonly avatar: HTMLElement;
  private readonly suggestions: HTMLElement;
  private readonly notice: HTMLElement;

  constructor(options: WidgetOptions) {
    this.root = options.root;
    this.host = options.host;
    this.doc = options.host.ownerDocument;
    this.api = options.api;
    this.sessions = options.sessions;
    this.now = options.now ?? (() => Date.now());
    this.session = this.sessions.load();

    const el = <K extends keyof HTMLElementTagNameMap>(tag: K, className = "") => {
      const node = this.doc.createElement(tag);
      if (className) node.className = className;
      return node;
    };

    const style = el("style");
    style.textContent = widgetCss();

    this.launcher = el("button", "launcher");
    this.launcher.type = "button";
    this.launcher.setAttribute("aria-expanded", "false");
    this.launcher.setAttribute("aria-controls", "bap-panel");
    this.launcher.setAttribute("aria-label", "Open chat");
    this.launcher.appendChild(icon(this.doc, "chat"));

    this.panel = el("section", "panel");
    this.panel.id = "bap-panel";
    this.panel.setAttribute("role", "dialog");
    this.panel.setAttribute("aria-label", "Chat");

    const header = el("header", "header");
    this.avatar = el("div", "avatar");
    this.avatar.setAttribute("aria-hidden", "true");
    const title = el("div", "title");
    this.nameEl = el("div", "name");
    const status = el("div", "status");
    const dot = el("span", "dot");
    dot.setAttribute("aria-hidden", "true");
    status.append(dot, this.doc.createTextNode("Online"));
    title.append(this.nameEl, status);
    this.closeButton = el("button", "icon-button");
    this.closeButton.type = "button";
    this.closeButton.setAttribute("aria-label", "Close chat");
    this.closeButton.appendChild(icon(this.doc, "close"));
    header.append(this.avatar, title, this.closeButton);

    this.notice = el("div", "notice");
    this.notice.setAttribute("role", "note");
    this.notice.hidden = true;

    this.list = el("div", "messages");
    this.list.setAttribute("role", "log");
    this.list.setAttribute("aria-label", "Conversation");
    // Streaming would make a live log re-announce every token; replies are announced whole
    // through the separate live region instead.
    this.list.setAttribute("aria-live", "off");
    this.list.tabIndex = 0;

    this.suggestions = el("div", "suggestions");

    const form = el("form", "composer");
    const label = el("label", "sr-only");
    label.htmlFor = "bap-input";
    label.textContent = "Message";
    this.input = el("textarea");
    this.input.id = "bap-input";
    this.input.rows = 1;
    this.input.maxLength = 2000;
    this.input.placeholder = "Type your message…";
    this.input.setAttribute("autocomplete", "off");
    this.sendButton = el("button", "send");
    this.sendButton.type = "submit";
    this.sendButton.setAttribute("aria-label", "Send message");
    this.sendButton.appendChild(icon(this.doc, "send"));
    form.append(label, this.input, this.sendButton);

    this.live = el("div", "sr-only");
    this.live.setAttribute("aria-live", "polite");
    this.live.setAttribute("aria-atomic", "true");

    this.panel.append(header, this.notice, this.list, this.suggestions, form, this.live);
    this.root.append(style, this.panel, this.launcher);

    this.launcher.addEventListener("click", () => (this.isOpen ? this.close() : void this.open()));
    this.closeButton.addEventListener("click", () => this.close());
    this.root.addEventListener("keydown", (event) => {
      if ((event as KeyboardEvent).key === "Escape" && this.isOpen) {
        event.preventDefault();
        this.close();
      }
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      void this.send(this.input.value);
    });
    this.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        void this.send(this.input.value);
      }
    });
    this.input.addEventListener("input", () => this.autosize());
    this.applyConfig(this.config);
  }

  get isOpen(): boolean {
    return this.panel.hasAttribute("data-open");
  }

  async open(): Promise<void> {
    if (this.isOpen) return;
    this.panel.setAttribute("data-open", "");
    this.host.setAttribute("data-open", "");
    this.launcher.setAttribute("aria-expanded", "true");
    this.launcher.setAttribute("aria-label", `Close chat with ${this.config.assistant_name}`);
    this.input.focus();
    if (!this.configLoaded) {
      this.configLoaded = true;
      try {
        this.applyConfig(await this.api.config());
      } catch (error) {
        if (error instanceof ApiError && error.status === 403) {
          this.showError("This chat is not available on this website.", false);
          this.setBusy(true);
          return;
        }
        // Keep the defaults; chatting can still work.
      }
      this.renderHistory();
    }
  }

  close(): void {
    if (!this.isOpen) return;
    this.panel.removeAttribute("data-open");
    this.host.removeAttribute("data-open");
    this.launcher.setAttribute("aria-expanded", "false");
    this.launcher.setAttribute("aria-label", `Open chat with ${this.config.assistant_name}`);
    this.launcher.focus();
  }

  private applyConfig(config: WidgetConfig): void {
    this.config = { ...DEFAULT_CONFIG, ...config };
    const accent = safeAccent(config.accent_color, tokens.color.accent);
    this.host.style.setProperty("--bap-accent", accent);
    this.host.style.setProperty("--bap-accent-text", readableTextOn(accent));
    const name = this.config.assistant_name || DEFAULT_CONFIG.assistant_name;
    this.nameEl.textContent = name;
    this.avatar.textContent = name.trim().charAt(0).toUpperCase() || "A";
    this.panel.setAttribute("aria-label", `Chat with ${name}`);
    this.notice.hidden = !config.offline;
    this.notice.textContent = config.offline
      ? "Demo mode: replies come from an offline test model."
      : "";
    this.launcher.setAttribute(
      "aria-label",
      `${this.isOpen ? "Close" : "Open"} chat with ${name}`,
    );
  }

  private renderHistory(): void {
    this.list.replaceChildren();
    if (this.config.greeting) this.addMessage("assistant", this.config.greeting, null);
    for (const message of this.session.messages) {
      const node = this.addMessage(message.role, message.text, message.at);
      if (message.citations?.length) this.addChips(node, message.citations);
    }
    this.renderSuggestions();
  }

  private renderSuggestions(): void {
    this.suggestions.replaceChildren();
    if (this.session.messages.length > 0) return;
    for (const question of this.config.suggested_questions.slice(0, 4)) {
      const button = this.doc.createElement("button");
      button.type = "button";
      button.className = "suggestion";
      button.textContent = question;
      button.addEventListener("click", () => {
        void this.send(question);
        this.keepFocus(); // the clicked suggestion is removed when the message is sent
      });
      this.suggestions.appendChild(button);
    }
  }

  private time(at: number): string {
    try {
      return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(at);
    } catch {
      return "";
    }
  }

  private addMessage(role: "user" | "assistant", text: string, at: number | null): HTMLElement {
    const node = this.doc.createElement("div");
    node.className = `msg ${role}`;
    const bubble = this.doc.createElement("div");
    bubble.className = "bubble";
    bubble.dir = "auto";
    bubble.appendChild(renderText(this.doc, text, { markers: role === "assistant" }));
    node.appendChild(bubble);
    if (at !== null) this.addMeta(node, at);
    this.list.appendChild(node);
    this.scrollToEnd();
    return node;
  }

  private addMeta(node: HTMLElement, at: number): void {
    const meta = this.doc.createElement("div");
    meta.className = "meta";
    const time = this.doc.createElement("time");
    time.dateTime = new Date(at).toISOString();
    time.textContent = this.time(at);
    meta.appendChild(time);
    node.appendChild(meta);
  }

  private addChips(node: HTMLElement, chips: SourceChip[]): void {
    const list = this.doc.createElement("ul");
    list.className = "sources";
    list.setAttribute("aria-label", "Sources");
    const seen = new Set<string>();
    for (const chip of chips) {
      const label = chip.detail ? `${chip.title} · ${chip.detail}` : chip.title;
      if (seen.has(label)) continue;
      seen.add(label);
      const item = this.doc.createElement("li");
      item.className = "chip";
      if (chip.snippet) item.title = chip.snippet;
      const text = this.doc.createElement("span");
      text.textContent = label;
      item.append(icon(this.doc, "doc"), text);
      list.appendChild(item);
    }
    node.appendChild(list);
  }

  /** If the focused control was removed, keep keyboard focus inside the panel. */
  private keepFocus(): void {
    const active = this.root.activeElement;
    if (this.isOpen && (!active || !active.isConnected)) this.input.focus();
  }

    private scrollToEnd(): void {
    this.list.scrollTop = this.list.scrollHeight;
  }

  private autosize(): void {
    this.input.style.height = "auto";
    this.input.style.height = `${Math.min(this.input.scrollHeight, 120)}px`;
  }

  private setBusy(busy: boolean): void {
    this.busy = busy;
    this.sendButton.disabled = busy;
  }

  private persist(): void {
    this.sessions.save(this.session);
  }

  private showError(text: string, canRetry: boolean, retryAfter: number | null = null): void {
    const node = this.doc.createElement("div");
    node.className = "msg error";
    node.setAttribute("role", "alert");
    const bubble = this.doc.createElement("div");
    bubble.className = "bubble";
    bubble.dir = "auto";
    bubble.appendChild(renderText(this.doc, text, { markers: false }));
    node.appendChild(bubble);
    if (canRetry) {
      const retry = this.doc.createElement("button");
      retry.type = "button";
      retry.className = "retry";
      retry.textContent = "Try again";
      if (retryAfter) {
        retry.disabled = true;
        setTimeout(() => (retry.disabled = false), Math.min(retryAfter, 60) * 1000);
      }
      retry.addEventListener("click", () => {
        node.remove();
        this.keepFocus();
        if (this.lastQuestion) void this.ask(this.lastQuestion.text, this.lastQuestion.clientId);
      });
      node.appendChild(retry);
    }
    this.list.appendChild(node);
    this.scrollToEnd();
  }

  /** Send a new customer message. */
  async send(raw: string): Promise<void> {
    const text = raw.trim();
    if (!text || this.busy) return;
    if (!this.isOpen) await this.open();
    this.input.value = "";
    this.autosize();
    const at = this.now();
    const clientId = newVisitorId();
    this.session.messages.push({ role: "user", text, at, clientId });
    this.persist();
    this.suggestions.replaceChildren();
    this.addMessage("user", text, at);
    await this.ask(text, clientId);
  }

  private async ask(text: string, clientId: string, retriedWithoutConversation = false): Promise<void> {
    this.lastQuestion = { text, clientId };
    this.setBusy(true);
    const node = this.doc.createElement("div");
    node.className = "msg assistant";
    const bubble = this.doc.createElement("div");
    bubble.className = "bubble";
    bubble.dir = "auto";
    const typing = this.doc.createElement("span");
    typing.className = "typing";
    typing.setAttribute("aria-hidden", "true");
    typing.append(this.doc.createElement("i"), this.doc.createElement("i"), this.doc.createElement("i"));
    bubble.appendChild(typing);
    node.appendChild(bubble);
    this.list.appendChild(node);
    this.scrollToEnd();
    this.live.textContent = `${this.config.assistant_name} is typing`;

    let reply = "";
    let chips: SourceChip[] = [];
    try {
      await this.api.chat(
        {
          visitor_id: this.session.visitorId,
          message: text,
          client_message_id: clientId,
          ...(this.session.conversationId ? { conversation_id: this.session.conversationId } : {}),
        },
        {
          onToken: (token) => {
            reply += token;
            bubble.replaceChildren(renderText(this.doc, reply));
            this.scrollToEnd();
          },
          onCitations: (citations) => {
            chips = citations.map(sourceChip);
          },
          onDone: (done) => {
            const at = this.now();
            this.session.conversationId = done.conversation_id;
            this.session.messages.push({ role: "assistant", text: reply, at, citations: chips });
            this.persist();
            this.addMeta(node, at);
            if (chips.length) this.addChips(node, chips);
            this.live.textContent = `${this.config.assistant_name}: ${reply}`;
          },
          onError: (error) => {
            node.remove();
            if (error.conversation_id) this.session.conversationId = error.conversation_id;
            this.persist();
            this.showError(error.message || "Sorry, something went wrong.", true);
            this.live.textContent = error.message || "Sorry, something went wrong.";
          },
        },
      );
    } catch (error) {
      node.remove();
      if (error instanceof ApiError && error.status === 404 && !retriedWithoutConversation) {
        // The conversation no longer exists on the server: start a fresh one.
        this.session.conversationId = null;
        this.persist();
        this.setBusy(false);
        return this.ask(text, clientId, true);
      }
      const message = this.describe(error);
      this.showError(message.text, message.retry, message.retryAfter);
      this.live.textContent = message.text;
    } finally {
      this.setBusy(false);
      this.scrollToEnd();
    }
  }

  private describe(error: unknown): { text: string; retry: boolean; retryAfter: number | null } {
    if (error instanceof ApiError) {
      if (error.status === 429) {
        // The server's message is already friendly ("...please wait a moment").
        const base = /[.!?]$/.test(error.message) ? error.message : `${error.message}.`;
        const seconds = error.retryAfter === 1 ? "1 second" : `${error.retryAfter} seconds`;
        const wait = error.retryAfter ? ` You can try again in ${seconds}.` : "";
        return { text: `${base}${wait}`, retry: true, retryAfter: error.retryAfter };
      }
      if (error.status === 403) {
        return { text: "This chat is not available on this website.", retry: false, retryAfter: null };
      }
      if (error.status === 422) {
        return { text: "That message couldn't be sent. Please shorten it and try again.", retry: false, retryAfter: null };
      }
    }
    return {
      text: "Sorry, we couldn't reach the assistant. Check your connection and try again.",
      retry: true,
      retryAfter: null,
    };
  }
}
