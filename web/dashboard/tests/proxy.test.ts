import { describe, expect, it } from "vitest";
import { allowed, csrfOk } from "@/lib/proxy";

const ID = "3f2b8a64-9c1e-4d5f-8a7b-1c2d3e4f5a6b";

describe("proxy allowlist", () => {
  it.each([
    ["GET", "analytics/overview"],
    ["GET", "conversations"],
    ["GET", `conversations/${ID}`],
    ["POST", `conversations/${ID}/messages`],
    ["POST", `conversations/${ID}/hand-back`],
    ["POST", "documents"],
    ["DELETE", `documents/${ID}`],
    ["POST", "knowledge-gaps/answer"],
    ["GET", `messages/${ID}/trace`],
    ["PATCH", "tenant/settings"],
    ["POST", "webhooks/test"],
    ["GET", "channels/telegram/status"],
    ["DELETE", `api-keys/${ID}`],
  ])("allows %s %s", (method, path) => {
    expect(allowed(method, path)).toBe(true);
  });

  it.each([
    ["POST", "chat"], // the customer chat endpoint is not a dashboard action
    ["GET", "chat/updates"],
    ["DELETE", "conversations"],
    ["GET", `conversations/${ID}/../../tenant`],
    ["PATCH", "tenant"],
    ["GET", "orders"],
    ["POST", "search"],
    ["GET", "conversations/not-a-uuid"],
  ])("refuses %s %s", (method, path) => {
    expect(allowed(method, path)).toBe(false);
  });
});

describe("CSRF check", () => {
  const headers = (h: Record<string, string>) => new Headers(h);
  it("lets reads through and needs the header plus the same origin for writes", () => {
    expect(csrfOk("GET", headers({}), "dash.example")).toBe(true);
    expect(csrfOk("POST", headers({}), "dash.example")).toBe(false);
    expect(csrfOk("POST", headers({ "x-bap-dashboard": "1", origin: "https://evil.example" }), "dash.example")).toBe(false);
    expect(csrfOk("POST", headers({ "x-bap-dashboard": "1", origin: "https://dash.example" }), "dash.example")).toBe(true);
    expect(csrfOk("DELETE", headers({ "x-bap-dashboard": "1" }), "dash.example")).toBe(true);
  });
});
