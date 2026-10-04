/** Screenshots of every screen with the demo data, for the README (npm run screenshots). They
 * contain no keys (keys are listed masked) and only fictional demo customers. */
import { type Page, expect, test } from "@playwright/test";
import { signIn } from "./helpers";
import { API, state } from "./stack";

const OUT = "../../docs/screenshots";

async function shot(page: Page, name: string) {
  await page.waitForLoadState("networkidle");
  const text = await page.locator("body").innerText();
  expect(text).not.toMatch(/bap_(admin|widget)_[A-Za-z0-9_-]{12,}/); // never a full key
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true });
}

test("capture every screen", async ({ page }) => {
  const headers = { Authorization: `Bearer ${state().key}` };
  const list = await (await fetch(`${API}/v1/conversations?q=Kacchi`, { headers })).json();
  const detail = await (await fetch(`${API}/v1/conversations/${list[0].id}`, { headers })).json();
  const reply = detail.messages.find((m: { role: string }) => m.role === "assistant");

  await page.goto("/login");
  await shot(page, "01-sign-in");
  await signIn(page);
  await shot(page, "02-overview");
  await page.goto("/inbox");
  await shot(page, "03-inbox");
  const waiting = await (await fetch(`${API}/v1/conversations?status=human`, { headers })).json();
  await page.goto(`/inbox/${(waiting[0] ?? list[0]).id}`);
  await shot(page, "04-conversation");
  await page.goto(`/traces/${reply.id}`);
  await shot(page, "05-run-trace");
  await page.goto("/knowledge");
  await shot(page, "06-knowledge");
  await page.goto("/gaps");
  await shot(page, "07-knowledge-gaps");
  await page.goto("/bookings");
  await shot(page, "08-bookings-and-leads");
  await page.goto("/settings");
  await shot(page, "09-settings");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/inbox");
  await page.screenshot({ path: `${OUT}/10-inbox-phone.png` });
});
