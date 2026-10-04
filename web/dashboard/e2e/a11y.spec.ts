import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { signIn } from "./helpers";
import { API, state } from "./stack";

async function firstConversation(): Promise<{ conversation: string; message: string }> {
  const headers = { Authorization: `Bearer ${state().key}` };
  const list = await (await fetch(`${API}/v1/conversations?q=Kacchi`, { headers })).json();
  const detail = await (await fetch(`${API}/v1/conversations/${list[0].id}`, { headers })).json();
  const reply = detail.messages.find((m: { role: string }) => m.role === "assistant");
  return { conversation: list[0].id, message: reply.id };
}

test("the sign-in page has no accessibility violations", async ({ page }) => {
  await page.goto("/login");
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
});

for (const path of ["/", "/inbox", "/knowledge", "/gaps", "/bookings", "/settings", "CONVERSATION", "TRACE"]) {
  test(`${path} has no accessibility violations`, async ({ page }) => {
    await signIn(page);
    const ids = path === "CONVERSATION" || path === "TRACE" ? await firstConversation() : null;
    const target = path === "CONVERSATION" ? `/inbox/${ids?.conversation}` : path === "TRACE" ? `/traces/${ids?.message}` : path;
    await page.goto(target);
    await page.waitForLoadState("networkidle");
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(" ")).join(", ")})`)).toEqual([]);
  });
}

test("keyboard: skip link and visible focus", async ({ page }) => {
  await signIn(page);
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Tab");
  const outline = await page.evaluate(() => getComputedStyle(document.activeElement as Element).outlineStyle);
  expect(outline).not.toBe("none");
});
