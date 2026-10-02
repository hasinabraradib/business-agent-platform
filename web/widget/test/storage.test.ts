import "./setup.ts";
import assert from "node:assert/strict";
import { test } from "node:test";
import { createStore, MemoryStore, SessionStore } from "../src/storage.ts";

test("visitor id and conversation survive a reload (same storage, new store object)", () => {
  const kv = new MemoryStore();
  const first = new SessionStore(kv, "ns");
  const session = first.load();
  session.conversationId = "conv-1";
  session.messages.push({ role: "user", text: "Hi", at: 1 });
  first.save(session);

  const reloaded = new SessionStore(kv, "ns").load();
  assert.equal(reloaded.visitorId, session.visitorId);
  assert.equal(reloaded.conversationId, "conv-1");
  assert.deepEqual(reloaded.messages, [{ role: "user", text: "Hi", at: 1 }]);
  assert.match(reloaded.visitorId, /^[A-Za-z0-9_.:-]+$/); // accepted by the API
});

test("widgets with different keys keep separate sessions", () => {
  const kv = new MemoryStore();
  const a = new SessionStore(kv, "key-a").load();
  const b = new SessionStore(kv, "key-b").load();
  assert.notEqual(a.visitorId, b.visitorId);
});

test("storage that throws falls back to memory without crashing", () => {
  const throwing = createStore(() => {
    throw new DOMException("denied", "SecurityError");
  });
  assert.ok(throwing instanceof MemoryStore);
  const full = createStore(
    () =>
      ({
        setItem() {
          throw new DOMException("full", "QuotaExceededError");
        },
        getItem: () => null,
        removeItem() {},
      }) as unknown as Storage,
  );
  assert.ok(full instanceof MemoryStore);
  const session = new SessionStore(throwing, "ns").load();
  assert.ok(session.visitorId);
});

test("localStorage is used when it works, and corrupt entries are replaced", () => {
  window.localStorage.clear();
  const kv = createStore();
  assert.ok(!(kv instanceof MemoryStore));
  window.localStorage.setItem("bap-widget:ns", "{not json");
  const session = new SessionStore(kv, "ns").load();
  assert.ok(session.visitorId);
  assert.equal(JSON.parse(window.localStorage.getItem("bap-widget:ns") ?? "{}").visitorId, session.visitorId);
});

test("only the most recent 50 messages are kept", () => {
  const kv = new MemoryStore();
  const store = new SessionStore(kv, "ns");
  const session = store.load();
  for (let i = 0; i < 70; i++) session.messages.push({ role: "user", text: `m${i}`, at: i });
  store.save(session);
  const reloaded = new SessionStore(kv, "ns").load();
  assert.equal(reloaded.messages.length, 50);
  assert.equal(reloaded.messages[0].text, "m20");
});
