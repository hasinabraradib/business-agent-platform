import assert from "node:assert/strict";
import { test } from "node:test";
import { MemoryStore } from "../src/storage.ts";
import { bubbles, CONFIG, flush, mountWidget, sse } from "./helpers.ts";

const CITATIONS = [
  {
    marker: 1,
    chunk_id: "c1",
    document_title: "Nodi Kitchen menu",
    metadata: { row: 1 },
    snippet: "dish_en: Kacchi Biryani",
  },
  {
    marker: 2,
    chunk_id: "c2",
    document_title: "Nodi Kitchen: hours",
    metadata: { section: "Nodi Kitchen > Opening hours" },
    snippet: "We are open every day",
  },
];

function reply(tokens: string[], citations = CITATIONS, conversation = "conv-1") {
  return sse([
    ...tokens.map((text): [string, unknown] => ["token", { text }]),
    ["citations", { citations }],
    ["done", { message_id: "m1", conversation_id: conversation, outcome: "answered", usage: {} }],
  ]);
}

test("opening loads the tenant config: name, greeting, accent and suggestions", async () => {
  const { widget, root, host, calls } = mountWidget([]);
  await widget.open();
  assert.equal(calls[0].url, "https://api.example/v1/widget/config");
  const headers = (calls[0].init?.headers ?? {}) as Record<string, string>;
  assert.equal(headers.Authorization, "Bearer bap_widget_testkey123456");
  assert.equal(root.querySelector(".name")?.textContent, "Nodi");
  assert.deepEqual(bubbles(root, "assistant").map((b) => b.textContent), [CONFIG.greeting]);
  assert.deepEqual(
    [...root.querySelectorAll(".suggestion")].map((b) => b.textContent),
    CONFIG.suggested_questions,
  );
  assert.equal(host.style.getPropertyValue("--bap-accent"), "#C5EE4F");
  assert.equal(host.style.getPropertyValue("--bap-accent-text"), "#111111"); // lime -> dark
});

test("a dark accent gets white bubble text", async () => {
  const { widget, host } = mountWidget([], { config: { ...CONFIG, accent_color: "#B5432F" } });
  await widget.open();
  assert.equal(host.style.getPropertyValue("--bap-accent"), "#B5432F");
  assert.equal(host.style.getPropertyValue("--bap-accent-text"), "#FFFFFF");
});

test("an invalid accent from the server is ignored", async () => {
  const { widget, host } = mountWidget([], {
    config: { ...CONFIG, accent_color: "red;background:url(https://evil.example)" },
  });
  await widget.open();
  assert.equal(host.style.getPropertyValue("--bap-accent"), "#C5EE4F");
});

test("streamed tokens append in order and citations become chips", async () => {
  const tokens = ["The Kacchi ", "Biryani is ", "BDT 480 [1]. ", "শুক্রবার ", "২:৩০ [2]"];
  const { widget, root, calls } = mountWidget([{ sse: reply(tokens), chunkSize: 5 }]);
  await widget.open();
  await widget.send("How much is the Kacchi?");

  const request = calls[1];
  assert.equal(request.url, "https://api.example/v1/chat");
  assert.equal(request.init?.method, "POST");
  assert.equal(request.body?.message, "How much is the Kacchi?");
  assert.equal(request.body?.stream, true);
  assert.ok(request.body?.visitor_id);
  assert.equal(request.body?.conversation_id, undefined); // first message
  assert.match(String(request.body?.client_message_id), /^[A-Za-z0-9_-]{8,64}$/);

  assert.deepEqual(bubbles(root, "user").map((b) => b.textContent), ["How much is the Kacchi?"]);
  const answer = bubbles(root, "assistant").at(-1);
  assert.equal(answer?.textContent, "The Kacchi Biryani is BDT 480 1. শুক্রবার ২:৩০ 2");
  const markers = answer ? [...answer.querySelectorAll("sup.cite")] : [];
  assert.deepEqual(markers.map((sup) => sup.textContent), ["1", "2"]);
  const chips = [...root.querySelectorAll(".msg.assistant .chip")];
  assert.deepEqual(chips.map((c) => c.textContent), [
    "Nodi Kitchen menu · row 1",
    "Nodi Kitchen: hours · Opening hours",
  ]);
  assert.equal(chips[0].getAttribute("title"), "dish_en: Kacchi Biryani");
  assert.ok(root.querySelector(".msg.assistant .meta time")?.textContent);
  assert.equal(root.querySelector(".typing"), null);
  assert.equal(
    root.querySelector('[aria-live="polite"]')?.textContent,
    "Nodi: The Kacchi Biryani is BDT 480 [1]. শুক্রবার ২:৩০ [2]",
  );
  assert.equal(root.querySelectorAll(".suggestion").length, 0);
});

test("tokens are rendered progressively while streaming", async () => {
  const { widget, root } = mountWidget([{ sse: reply(["one ", "two ", "three"]), chunkSize: 3 }]);
  await widget.open();
  const seen: string[] = [];
  const observer = new MutationObserver(() => {
    const text = bubbles(root, "assistant").at(-1)?.textContent ?? "";
    if (text && seen.at(-1) !== text) seen.push(text);
  });
  observer.observe(root, { subtree: true, childList: true, characterData: true });
  await widget.send("count");
  await flush();
  observer.disconnect();
  const progress = seen.filter((t) => ["one ", "one two ", "one two three"].includes(t));
  assert.deepEqual(progress, ["one ", "one two ", "one two three"]);
});

test("an HTML or script payload in a reply is shown as text", async () => {
  const payload = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
  const { widget, root } = mountWidget([
    {
      sse: reply([payload], [
        { ...CITATIONS[0], document_title: "<b>evil</b>", snippet: "<script>x</script>" },
      ]),
    },
  ]);
  await widget.open();
  await widget.send("<i>hello</i>");
  const answer = bubbles(root, "assistant").at(-1);
  assert.equal(answer?.textContent, payload);
  assert.equal(root.querySelector(".messages img, .messages script, .messages b, .messages i"), null);
  assert.equal(bubbles(root, "user")[0].textContent, "<i>hello</i>");
  assert.equal(root.querySelector(".chip")?.textContent, "<b>evil</b> · row 1");
});

test("the visitor and conversation survive a reload, and the history is shown", async () => {
  const kv = new MemoryStore();
  const first = mountWidget([{ sse: reply(["Hello [1]"]) }], { kv });
  await first.widget.open();
  await first.widget.send("Hi");
  const visitor = first.calls[1].body?.visitor_id;

  const second = mountWidget([{ sse: reply(["Again"], [], "conv-1") }], { kv });
  await second.widget.open();
  assert.deepEqual(bubbles(second.root, "user").map((b) => b.textContent), ["Hi"]);
  assert.deepEqual(bubbles(second.root, "assistant").map((b) => b.textContent), [
    CONFIG.greeting,
    "Hello 1",
  ]);
  assert.equal(second.root.querySelectorAll(".chip").length, 2);
  assert.equal(second.root.querySelectorAll(".suggestion").length, 0);
  await second.widget.send("And now?");
  assert.equal(second.calls[1].body?.visitor_id, visitor);
  assert.equal(second.calls[1].body?.conversation_id, "conv-1");
});

test("unavailable storage does not crash the widget", async () => {
  const failing = {
    get(): string | null {
      throw new DOMException("denied", "SecurityError");
    },
    set(): void {
      throw new DOMException("full", "QuotaExceededError");
    },
  };
  const { widget, root } = mountWidget([{ sse: reply(["Fine"]) }], { kv: failing });
  await widget.open();
  await widget.send("Hi");
  assert.equal(bubbles(root, "assistant").at(-1)?.textContent, "Fine");
});

test("a server error event shows the apology with a retry button that works", async () => {
  const apology = "Sorry, I'm having trouble answering right now. You can also reach us: call 01700";
  const { widget, root, calls } = mountWidget([
    { sse: sse([["token", { text: "partial" }], ["error", { message: apology, conversation_id: "conv-9" }]]) },
    { sse: reply(["Recovered"], [], "conv-9") },
  ]);
  await widget.open();
  await widget.send("Kacchi price?");
  const error = root.querySelector(".msg.error");
  assert.equal(error?.getAttribute("role"), "alert");
  assert.equal(error?.querySelector(".bubble")?.textContent, apology);
  assert.ok(!bubbles(root, "assistant").some((b) => b.textContent === "partial"));

  (root.querySelector(".msg.error .retry") as HTMLButtonElement).click();
  await flush();
  await flush();
  assert.equal(root.querySelector(".msg.error"), null);
  assert.equal(bubbles(root, "assistant").at(-1)?.textContent, "Recovered");
  assert.equal(calls[2].body?.message, "Kacchi price?"); // the same question, re-asked
  // ...with the same client_message_id, so the server stores the question only once.
  assert.equal(calls[2].body?.client_message_id, calls[1].body?.client_message_id);
  assert.equal(calls[2].body?.conversation_id, "conv-9");
  assert.equal(bubbles(root, "user").length, 1); // no duplicate customer bubble
});

test("a network failure shows an error state with retry", async () => {
  const { widget, root } = mountWidget([{ reject: true }]);
  await widget.open();
  await widget.send("Hello?");
  const error = root.querySelector(".msg.error");
  assert.match(error?.textContent ?? "", /couldn't reach the assistant/);
  assert.equal((root.querySelector(".msg.error .retry") as HTMLButtonElement).disabled, false);
  assert.equal(widget.sendButton.disabled, false); // can type again
});

test("429 shows a friendly message and holds the retry button briefly", async () => {
  const { widget, root } = mountWidget([
    {
      status: 429,
      json: { detail: "You're sending messages too quickly; please wait a moment" },
      headers: { "Retry-After": "1" },
    },
  ]);
  await widget.open();
  await widget.send("Hi");
  const error = root.querySelector(".msg.error");
  assert.equal(
    error?.querySelector(".bubble")?.textContent,
    "You're sending messages too quickly; please wait a moment. You can try again in 1 second.",
  );
  assert.equal((root.querySelector(".msg.error .retry") as HTMLButtonElement).disabled, true);
});

test("a disallowed website gets a clear message and cannot send", async () => {
  const { widget, root } = mountWidget([], {
    config: { status: 403, json: { detail: "This website is not allowed to use this chat widget" } },
  });
  await widget.open();
  assert.match(root.querySelector(".msg.error")?.textContent ?? "", /not available on this website/);
  assert.equal(root.querySelector(".msg.error .retry"), null);
  assert.equal(widget.sendButton.disabled, true);
});

test("a vanished conversation is restarted once without the old id", async () => {
  const kv = new MemoryStore();
  kv.set(
    "bap-widget:testkey123456",
    JSON.stringify({ visitorId: "v-1", conversationId: "gone", messages: [] }),
  );
  const { widget, root, calls } = mountWidget(
    [{ status: 404, json: { detail: "Conversation not found" } }, { sse: reply(["Fresh"], [], "conv-new") }],
    { kv },
  );
  await widget.open();
  await widget.send("Hi");
  assert.equal(calls[1].body?.conversation_id, "gone");
  assert.equal(calls[2].body?.conversation_id, undefined);
  assert.equal(bubbles(root, "assistant").at(-1)?.textContent, "Fresh");
  assert.equal(JSON.parse(kv.get("bap-widget:testkey123456") ?? "{}").conversationId, "conv-new");
});

test("keyboard: open moves focus to the input, Escape closes and returns focus", async () => {
  const { widget, root, host } = mountWidget([]);
  assert.equal(widget.launcher.getAttribute("aria-expanded"), "false");
  widget.launcher.click();
  await flush();
  assert.ok(widget.isOpen);
  assert.equal(widget.launcher.getAttribute("aria-expanded"), "true");
  assert.equal(host.hasAttribute("data-open"), true);
  assert.equal(root.activeElement, widget.input);

  widget.panel.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.equal(widget.isOpen, false);
  assert.equal(widget.launcher.getAttribute("aria-expanded"), "false");
  assert.equal(root.activeElement, widget.launcher);
  assert.equal(widget.launcher.getAttribute("aria-label"), "Open chat with Nodi");
});

test("Enter sends, Shift+Enter does not; empty messages are ignored", async () => {
  const { widget, calls } = mountWidget([{ sse: reply(["ok"]) }]);
  await widget.open();
  widget.input.value = "line one";
  widget.input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", shiftKey: true, bubbles: true }));
  assert.equal(calls.length, 1); // only the config call
  widget.input.value = "   ";
  widget.input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  assert.equal(calls.length, 1);
  widget.input.value = "Hello";
  widget.input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await flush();
  await flush();
  assert.equal(calls[1].body?.message, "Hello");
  assert.equal(widget.input.value, "");
});

test("accessible structure: dialog, labelled controls, live region", async () => {
  const { widget, root } = mountWidget([]);
  await widget.open();
  assert.equal(widget.panel.getAttribute("role"), "dialog");
  assert.equal(widget.panel.getAttribute("aria-label"), "Chat with Nodi");
  assert.equal(widget.closeButton.getAttribute("aria-label"), "Close chat");
  assert.equal(widget.sendButton.getAttribute("aria-label"), "Send message");
  assert.equal(root.querySelector('label[for="bap-input"]')?.textContent, "Message");
  assert.equal(widget.list.getAttribute("aria-live"), "off");
  assert.ok(root.querySelector('[aria-live="polite"][aria-atomic="true"]'));
  assert.ok(root.querySelector("style")?.textContent?.includes("prefers-reduced-motion"));
});

test("focus stays in the panel after a suggestion is used, so Escape still closes it", async () => {
  const { widget, root } = mountWidget([{ sse: reply(["Menu reply"]) }]);
  await widget.open();
  const suggestion = root.querySelector(".suggestion") as HTMLButtonElement;
  suggestion.focus();
  suggestion.click();
  await flush();
  await flush();
  assert.equal(root.querySelectorAll(".suggestion").length, 0);
  assert.equal(root.activeElement, widget.input);
  widget.input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, composed: true }));
  assert.equal(widget.isOpen, false);
  assert.equal(root.activeElement, widget.launcher);
});

test("after retrying, focus moves to the input instead of being lost", async () => {
  const { widget, root } = mountWidget([{ reject: true }, { sse: reply(["Back"]) }]);
  await widget.open();
  await widget.send("Hello?");
  const retry = root.querySelector(".msg.error .retry") as HTMLButtonElement;
  retry.focus();
  retry.click();
  await flush();
  await flush();
  assert.equal(root.activeElement, widget.input);
});

test("each new message gets its own client_message_id", async () => {
  const { widget, calls } = mountWidget([{ sse: reply(["one"]) }, { sse: reply(["two"]) }]);
  await widget.open();
  await widget.send("first");
  await widget.send("second");
  assert.notEqual(calls[1].body?.client_message_id, calls[2].body?.client_message_id);
});

test("offline mode shows one notice at the top of the panel", async () => {
  const offline = mountWidget([], { config: { ...CONFIG, offline: true } });
  await offline.widget.open();
  const notice = offline.root.querySelector(".notice") as HTMLElement;
  assert.equal(notice.hidden, false);
  assert.equal(notice.getAttribute("role"), "note");
  assert.match(notice.textContent ?? "", /offline test model/);

  const online = mountWidget([], { config: { ...CONFIG, offline: false } });
  await online.widget.open();
  assert.equal((online.root.querySelector(".notice") as HTMLElement).hidden, true);
});
