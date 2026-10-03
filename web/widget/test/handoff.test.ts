import assert from "node:assert/strict";
import { test } from "node:test";
import { bubbles, flush, mountWidget, sse } from "./helpers.ts";

const ACK = "I've passed this to our team. They're online now and will reply here shortly.";

function handoffReply(): string {
  return sse([
    ["token", { text: ACK }],
    ["citations", { citations: [] }],
    ["done", { message_id: "m1", conversation_id: "conv-1", outcome: "handoff", conversation_status: "waiting_human" }],
  ]);
}

const silent = sse([
  ["done", { message_id: null, conversation_id: "conv-1", outcome: null, silent: true, conversation_status: "waiting_human" }],
]);

const staffMessage = { id: "s1", content: "Hi, this is Rumana. How can I help?", created_at: "2026-10-03T13:31:00+00:00" };

test("after a handoff the widget shows team replies labelled Team member, never the assistant", async () => {
  const { widget, root, calls } = mountWidget([
    { sse: handoffReply() },
    { json: { conversation_status: "waiting_human", messages: [] } }, // first poll: nothing yet
    { json: { conversation_status: "human", messages: [staffMessage] } },
    { json: { conversation_status: "human", messages: [staffMessage] } }, // repeated: shown once
    { json: { conversation_status: "ai", messages: [] } }, // handed back: polling stops
  ]);
  await widget.open();
  await widget.send("Can I talk to a real person?");
  await flush();
  assert.equal(bubbles(root, "assistant").at(-1)?.textContent, ACK);

  const poll = calls[2];
  assert.equal(
    poll.url,
    "https://api.example/v1/chat/updates?visitor_id=" +
      encodeURIComponent(String(calls[1].body?.visitor_id)) + "&conversation_id=conv-1",
  );
  await widget.poll();
  const staff = [...root.querySelectorAll<HTMLElement>(".msg.staff")];
  assert.equal(staff.length, 1);
  assert.equal(staff[0].querySelector(".who")?.textContent, "Team member");
  assert.equal(staff[0].querySelector(".bubble")?.textContent, staffMessage.content);
  assert.ok(!staff[0].textContent?.includes("Nodi")); // no assistant name or AI label on it
  assert.equal(root.querySelector('[aria-live="polite"]')?.textContent, `Team member: ${staffMessage.content}`);
  assert.doesNotMatch(calls[3].url, /after=/);

  await widget.poll();
  assert.match(calls[4].url, /after=2026-10-03T13%3A31%3A00%2B00%3A00/); // only newer ones
  assert.equal(root.querySelectorAll(".msg.staff").length, 1);
  await widget.poll();
  const before = calls.length;
  await widget.poll(); // status is ai again: nothing is fetched by the timer any more
  assert.equal(calls.length, before + 1); // an explicit poll still asks once
  widget.close();
});

test("a silent reply leaves no assistant bubble and no typing indicator", async () => {
  const { widget, root, kv } = mountWidget([
    { sse: handoffReply() },
    { json: { conversation_status: "waiting_human", messages: [] } },
    { sse: silent },
    { json: { conversation_status: "waiting_human", messages: [] } },
  ]);
  await widget.open();
  await widget.send("Talk to a human");
  await widget.send("Hello? Is anyone there?");
  await flush();
  assert.deepEqual(bubbles(root, "user").map((b) => b.textContent), ["Talk to a human", "Hello? Is anyone there?"]);
  assert.deepEqual(bubbles(root, "assistant").map((b) => b.textContent), ["Assalamu alaikum! Ask me anything.", ACK]);
  assert.equal(root.querySelector(".typing"), null);
  const stored = JSON.parse(String(kv.get("bap-widget:testkey123456")));
  assert.equal(stored.status, "waiting_human");
  assert.deepEqual(stored.messages.map((m: { role: string }) => m.role), ["user", "assistant", "user"]);
  widget.close();
});

test("team replies survive a reload and keep their label", async () => {
  const first = mountWidget([
    { sse: handoffReply() },
    { json: { conversation_status: "human", messages: [staffMessage] } },
  ]);
  await first.widget.open();
  await first.widget.send("Talk to a human");
  await flush();
  await flush();
  first.widget.close();
  const second = mountWidget([{ json: { conversation_status: "human", messages: [] } }], { kv: first.kv });
  await second.widget.open();
  await flush();
  const staff = second.root.querySelector(".msg.staff");
  assert.equal(staff?.querySelector(".who")?.textContent, "Team member");
  assert.equal(staff?.querySelector(".bubble")?.textContent, staffMessage.content);
  assert.match(second.calls.at(-1)?.url ?? "", /after=2026-10-03T13%3A31%3A00/); // only newer ones
  second.widget.close();
});

test("no polling while the assistant is answering", async () => {
  const { widget, calls } = mountWidget([
    { sse: sse([["token", { text: "Hello!" }], ["done", { message_id: "m1", conversation_id: "conv-1", outcome: "smalltalk", conversation_status: "ai" }]]) },
  ]);
  await widget.open();
  await widget.send("hi");
  await flush();
  assert.deepEqual(calls.map((c) => c.url.replace("https://api.example", "")), ["/v1/widget/config", "/v1/chat"]);
  widget.close();
});
